import asyncio
import contextlib
import hashlib
import io
import json
import logging
import math
import os
import uuid
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, List
from urllib.parse import parse_qs, urlparse

import aiofiles
import httpx
import librosa
import numpy as np
import torch
import whisper
try:
    import psutil  # type: ignore[import-not-found]
except Exception:
    psutil = None
from openpyxl import Workbook
from scipy.io import wavfile
from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Подавляем warning про FP16 на CPU
warnings.filterwarnings("ignore", message=".*FP16 is not supported on CPU.*")

app = FastAPI(title="Voximplant Kit Call History MVP")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

AUDIO_DIR = Path("audio-cache")
AUDIO_DIR.mkdir(exist_ok=True)

MODELS_DIR = Path("whisper-models")
MODELS_DIR.mkdir(exist_ok=True)

TRANSCRIPTS_CACHE_FILE = Path("transcripts-cache.json")
SCHEDULES_FILE = Path("schedules.json")
QUEUE_FILE = Path("transcription-queue.json")

active_connections: Dict[str, WebSocket] = {}
active_sessions: Dict[str, dict] = {}
model_states: Dict[str, dict] = {}
loaded_models: Dict[str, object] = {}
loading_tasks: Dict[str, asyncio.Task] = {}
audio_download_locks: Dict[str, asyncio.Lock] = {}
DEFAULT_WHISPER_MODEL = "base"
SUPPORTED_WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3", "turbo"]
SEARCH_PAGE_SIZE = 50
MAX_SEARCH_PAGES = 10
MAX_SEARCH_PAGES_HARD_LIMIT = 500
SEARCH_RETRY_DELAY_SECONDS = 1.0
MAX_SEARCH_RETRIES = 2
LOGICAL_CPU_COUNT = max(1, os.cpu_count() or 1)
PHYSICAL_CPU_COUNT = max(1, (psutil.cpu_count(logical=False) if psutil else None) or LOGICAL_CPU_COUNT)
MAX_TRANSCRIBE_CONCURRENCY_HARD_LIMIT = 12
PERFORMANCE_PROFILE_MAX = "max"
PERFORMANCE_PROFILE_MODERATE = "moderate"
SUPPORTED_PERFORMANCE_PROFILES = {PERFORMANCE_PROFILE_MAX, PERFORMANCE_PROFILE_MODERATE}
DEFAULT_PERFORMANCE_PROFILE = (
    os.getenv("TRANSCRIBE_PERFORMANCE_PROFILE", PERFORMANCE_PROFILE_MODERATE).strip().lower()
)
if DEFAULT_PERFORMANCE_PROFILE not in SUPPORTED_PERFORMANCE_PROFILES:
    DEFAULT_PERFORMANCE_PROFILE = PERFORMANCE_PROFILE_MODERATE
# CPU inference on one model instance is safer in serialized mode.
WHISPER_INFERENCE_ISOLATION = os.getenv("WHISPER_INFERENCE_ISOLATION", "serialized").strip().lower()
if WHISPER_INFERENCE_ISOLATION not in {"concurrent", "serialized"}:
    WHISPER_INFERENCE_ISOLATION = "serialized"


def get_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return max(minimum, min(maximum, default))
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"[CONFIG] Некорректное значение {name}={raw}, использую {default}")
        value = default
    return max(minimum, min(maximum, value))


class DynamicConcurrencyLimiter:
    def __init__(self, max_concurrency: int):
        self._max_concurrency = max(1, int(max_concurrency))
        self._in_use = 0
        self._condition = asyncio.Condition()

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def available(self) -> int:
        return max(0, self._max_concurrency - self._in_use)

    async def resize(self, max_concurrency: int) -> None:
        new_value = max(1, int(max_concurrency))
        async with self._condition:
            self._max_concurrency = new_value
            self._condition.notify_all()

    async def acquire(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._in_use < self._max_concurrency)
            self._in_use += 1

    async def release(self) -> None:
        async with self._condition:
            self._in_use = max(0, self._in_use - 1)
            self._condition.notify_all()

    @contextlib.asynccontextmanager
    async def slot(self):
        await self.acquire()
        try:
            yield
        finally:
            await self.release()


def calculate_performance_plan(profile: str) -> Dict[str, int]:
    normalized_profile = profile if profile in SUPPORTED_PERFORMANCE_PROFILES else PERFORMANCE_PROFILE_MODERATE
    total_cores = max(1, PHYSICAL_CPU_COUNT)
    if normalized_profile == PERFORMANCE_PROFILE_MAX:
        usable_cores = total_cores
    else:
        usable_cores = max(1, int(math.floor(total_cores * 0.75)))

    reserved_cores = max(0, total_cores - usable_cores)

    if usable_cores <= 2:
        concurrency = 1
    elif usable_cores <= 4:
        concurrency = 2
    elif usable_cores <= 8:
        concurrency = min(4, usable_cores)
    else:
        concurrency = min(8, usable_cores)

    concurrency = max(1, min(MAX_TRANSCRIBE_CONCURRENCY_HARD_LIMIT, concurrency))
    torch_threads = max(1, usable_cores // concurrency)

    # Prefer at least 2 torch threads per task on CPU when possible.
    while concurrency > 1 and torch_threads < 2 and usable_cores >= 4:
        concurrency -= 1
        torch_threads = max(1, usable_cores // concurrency)

    queue_workers = max(1, min(concurrency, usable_cores))

    return {
        "profile": normalized_profile,
        "physical_cores": total_cores,
        "logical_cores": LOGICAL_CPU_COUNT,
        "usable_cores": usable_cores,
        "reserved_cores": reserved_cores,
        "max_concurrency": concurrency,
        "queue_workers": queue_workers,
        "torch_num_threads": torch_threads,
        "torch_num_interop_threads": 1,
    }


def apply_env_startup_overrides(plan: Dict[str, int]) -> Dict[str, int]:
    updated = {**plan}

    raw_concurrency = os.getenv("MAX_TRANSCRIBE_CONCURRENCY")
    if raw_concurrency is not None:
        try:
            requested = int(raw_concurrency)
            updated["max_concurrency"] = max(1, min(MAX_TRANSCRIBE_CONCURRENCY_HARD_LIMIT, requested))
        except ValueError:
            logger.warning(f"[CONFIG] Некорректное MAX_TRANSCRIBE_CONCURRENCY={raw_concurrency}, использую авто-значение")

    raw_threads = os.getenv("TORCH_NUM_THREADS")
    if raw_threads is not None:
        try:
            requested = int(raw_threads)
            updated["torch_num_threads"] = max(1, min(LOGICAL_CPU_COUNT, requested))
        except ValueError:
            logger.warning(f"[CONFIG] Некорректное TORCH_NUM_THREADS={raw_threads}, использую авто-значение")

    # Keep CPU utilization sane: concurrency * torch_threads should not heavily exceed usable cores.
    safe_threads_cap = max(1, updated["usable_cores"] // max(1, updated["max_concurrency"]))
    if updated["torch_num_threads"] > safe_threads_cap:
        logger.info(
            f"[CONFIG] Ограничиваю TORCH_NUM_THREADS до {safe_threads_cap} "
            f"для профиля {updated['profile']} (чтобы избежать oversubscription)"
        )
        updated["torch_num_threads"] = safe_threads_cap

    if os.getenv("TORCH_NUM_INTEROP_THREADS") not in {None, "", "1"}:
        logger.info("[CONFIG] TORCH_NUM_INTEROP_THREADS принудительно установлен в 1 для CPU-профиля")
    updated["torch_num_interop_threads"] = 1
    updated["queue_workers"] = max(1, min(updated["queue_workers"], updated["max_concurrency"]))
    return updated


runtime_performance = apply_env_startup_overrides(calculate_performance_plan(DEFAULT_PERFORMANCE_PROFILE))
TORCH_THREADS_CONFIGURED = False
STARTUP_TORCH_NUM_THREADS = get_env_int(
    "TORCH_NUM_THREADS",
    runtime_performance["torch_num_threads"],
    1,
    LOGICAL_CPU_COUNT,
)
STARTUP_TORCH_NUM_INTEROP_THREADS = 1
runtime_settings_lock = asyncio.Lock()
WHISPER_TRANSCRIBE_TIMEOUT_SECONDS = get_env_int("WHISPER_TRANSCRIBE_TIMEOUT_SECONDS", 420, 30, 3600)
WHISPER_FALLBACK_TIMEOUT_SECONDS = get_env_int("WHISPER_FALLBACK_TIMEOUT_SECONDS", 240, 30, 3600)
transcribe_limiter = DynamicConcurrencyLimiter(runtime_performance["max_concurrency"])
active_transcriptions = 0
active_inference_tasks = 0
model_inference_locks: Dict[str, asyncio.Lock] = {}
MIN_WHISPER_AUDIO_SECONDS = 1.0
MIN_AUDIO_FILE_BYTES = 100
MIN_AUDIO_SECONDS_FOR_TRANSCRIBE = 0.1
SCHEDULER_POLL_SECONDS = 30

WEEKDAY_NAME_TO_INDEX = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


class ProcessRequest(BaseModel):
    api_host: str
    domain: str
    access_token: str
    from_date: str
    to_date: str
    scenario_name: Optional[str] = ""
    scenario_id: Optional[int] = None
    min_duration: int = 0
    has_recording: bool = True
    session_id: str
    records_limit: int = 100  # Максимум записей для загрузки


class LoadMoreRequest(BaseModel):
    session_id: str


class ScheduleRequest(BaseModel):
    name: Optional[str] = ""
    weekdays: List[int]
    from_hour: int
    to_hour: int
    api_host: str
    domain: str
    access_token: str
    scenario_id: Optional[int] = None
    min_duration: int = 0
    has_recording: bool = True
    records_limit: int = 100
    whisper_model: str = DEFAULT_WHISPER_MODEL
    enabled: bool = True


class UpdateScheduleRequest(BaseModel):
    enabled: Optional[bool] = None
    name: Optional[str] = None


class QueueAddRequest(BaseModel):
    record: dict
    api_host: str
    domain: str
    access_token: str
    whisper_model: Optional[str] = DEFAULT_WHISPER_MODEL


class QueueAddBulkRequest(BaseModel):
    records: List[dict]
    api_host: str
    domain: str
    access_token: str
    whisper_model: Optional[str] = DEFAULT_WHISPER_MODEL


class QueueStopRequest(BaseModel):
    cancel_queued: bool = False


class QueueClearRequest(BaseModel):
    statuses: List[str] = ["done", "failed", "skipped"]


class PerformanceProfileRequest(BaseModel):
    profile: str



def resolve_api_host_and_account(api_host: str, domain: str):
    return (api_host or "").strip(), (domain or "").strip()


def normalize_whisper_model_name(model_name: Optional[str]) -> str:
    name = (model_name or DEFAULT_WHISPER_MODEL).strip().lower()
    if name in SUPPORTED_WHISPER_MODELS:
        return name
    return DEFAULT_WHISPER_MODEL


def is_multilingual_whisper_model(model_name: str) -> bool:
    return not model_name.endswith(".en")


def configure_torch_threads() -> None:
    global TORCH_THREADS_CONFIGURED
    if TORCH_THREADS_CONFIGURED:
        return

    try:
        torch.set_num_interop_threads(STARTUP_TORCH_NUM_INTEROP_THREADS)
        torch.set_num_threads(STARTUP_TORCH_NUM_THREADS)
        runtime_performance["torch_num_threads"] = STARTUP_TORCH_NUM_THREADS
        runtime_performance["torch_num_interop_threads"] = STARTUP_TORCH_NUM_INTEROP_THREADS
        TORCH_THREADS_CONFIGURED = True
        logger.info(
            f"[WHISPER] PyTorch threads configured once on startup: num_threads={STARTUP_TORCH_NUM_THREADS}, "
            f"interop_threads={STARTUP_TORCH_NUM_INTEROP_THREADS}, "
            f"max_transcribe_concurrency={runtime_performance['max_concurrency']}, "
            f"profile={runtime_performance['profile']}"
        )
    except Exception as exc:
        # PyTorch interop/thread settings are startup-only. Fail fast to avoid hidden hangs.
        logger.error(f"[WHISPER] Критическая ошибка инициализации PyTorch threads: {exc}")
        raise RuntimeError(
            "Не удалось один раз инициализировать PyTorch threads на старте приложения"
        ) from exc


async def apply_performance_profile(profile: str) -> Dict[str, Any]:
    normalized = profile.strip().lower()
    if normalized not in SUPPORTED_PERFORMANCE_PROFILES:
        raise HTTPException(status_code=400, detail="Недопустимый профиль производительности")

    new_plan = calculate_performance_plan(normalized)

    async with runtime_settings_lock:
        runtime_performance["profile"] = new_plan["profile"]
        runtime_performance["usable_cores"] = new_plan["usable_cores"]
        runtime_performance["reserved_cores"] = new_plan["reserved_cores"]
        runtime_performance["max_concurrency"] = new_plan["max_concurrency"]
        runtime_performance["queue_workers"] = new_plan["queue_workers"]
        await transcribe_limiter.resize(new_plan["max_concurrency"])

    return {
        "profile": runtime_performance["profile"],
        "torch_applied_now": False,
        "pending_torch_update": False,
        "torch_runtime_change_allowed": False,
        "note": "PyTorch threads фиксируются один раз при старте; профиль меняет только concurrency/worker pool.",
        "settings": {
            "max_concurrency": runtime_performance["max_concurrency"],
            "queue_workers": runtime_performance["queue_workers"],
            "torch_num_threads": runtime_performance["torch_num_threads"],
            "torch_num_interop_threads": runtime_performance["torch_num_interop_threads"],
        },
    }


def get_model_status(model_name: str) -> dict:
    cache_aliases = {
        "turbo": ["large-v3-turbo.pt", "turbo.pt"],
    }

    candidate_paths = [MODELS_DIR / model_name, MODELS_DIR / f"{model_name}.pt"]
    for alias_name in cache_aliases.get(model_name, []):
        candidate_paths.append(MODELS_DIR / alias_name)

    existing_path = None
    for candidate in candidate_paths:
        if candidate.exists():
            existing_path = candidate
            break

    state = model_states.get(model_name, {"status": "missing"})
    if state.get("status") == "missing" and existing_path:
        return {
            "status": "ready",
            "progress": 100,
            "error": None,
            "cache_path": str(existing_path),
        }

    if existing_path:
        return {
            **state,
            "cache_path": str(existing_path),
        }

    return state


def get_model_inference_lock(model_name: str) -> asyncio.Lock:
    lock = model_inference_locks.get(model_name)
    if lock is None:
        lock = asyncio.Lock()
        model_inference_locks[model_name] = lock
    return lock


@contextlib.asynccontextmanager
async def maybe_model_inference_lock(model_name: str):
    if WHISPER_INFERENCE_ISOLATION == "serialized":
        lock = get_model_inference_lock(model_name)
        async with lock:
            yield
    else:
        yield


def load_transcripts_cache() -> Dict[str, dict]:
    if not TRANSCRIPTS_CACHE_FILE.exists():
        return {}
    try:
        with TRANSCRIPTS_CACHE_FILE.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        if isinstance(payload, dict):
            return payload
        logger.warning("[CACHE] Файл transcripts-cache.json имеет неверный формат, кэш будет сброшен")
    except Exception as exc:
        logger.warning(f"[CACHE] Не удалось прочитать transcripts-cache.json: {exc}")
    return {}


def save_transcripts_cache() -> None:
    temp_path = TRANSCRIPTS_CACHE_FILE.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(persisted_transcripts, file, ensure_ascii=False, indent=2)
    temp_path.replace(TRANSCRIPTS_CACHE_FILE)


def load_schedules() -> Dict[str, dict]:
    if not SCHEDULES_FILE.exists():
        return {}
    try:
        with SCHEDULES_FILE.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        if isinstance(payload, dict):
            return payload
        logger.warning("[SCHEDULER] Файл schedules.json имеет неверный формат, расписание сброшено")
    except Exception as exc:
        logger.warning(f"[SCHEDULER] Не удалось прочитать schedules.json: {exc}")
    return {}


def save_schedules() -> None:
    temp_path = SCHEDULES_FILE.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(persisted_schedules, file, ensure_ascii=False, indent=2)
    temp_path.replace(SCHEDULES_FILE)


def load_queue() -> Dict[str, dict]:
    if not QUEUE_FILE.exists():
        return {}
    try:
        with QUEUE_FILE.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        if isinstance(payload, dict):
            normalized = {}
            for call_id, item in payload.items():
                if not isinstance(item, dict):
                    continue
                normalized_call_id = str(item.get("call_id") or call_id)
                status = str(item.get("status") or "queued")
                if status == "processing":
                    status = "queued"
                normalized[normalized_call_id] = {
                    **item,
                    "call_id": normalized_call_id,
                    "status": status,
                    "updated_at": item.get("updated_at") or datetime.utcnow().isoformat(timespec="seconds") + "Z",
                }
            return normalized
        logger.warning("[QUEUE] Файл transcription-queue.json имеет неверный формат, очередь будет сброшена")
    except Exception as exc:
        logger.warning(f"[QUEUE] Не удалось прочитать transcription-queue.json: {exc}")
    return {}


def save_queue() -> None:
    temp_path = QUEUE_FILE.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(persisted_queue, file, ensure_ascii=False, indent=2)
    temp_path.replace(QUEUE_FILE)


def get_cached_transcript(call_id: str, record_url: Optional[str] = None) -> Optional[dict]:
    entry = persisted_transcripts.get(str(call_id))
    if not entry:
        return None
    if record_url and entry.get("record_url") and entry.get("record_url") != record_url:
        return None
    return entry


def upsert_cached_transcript(call_id: str, transcript: str, record_url: str, whisper_model: str, audio_url: str) -> None:
    persisted_transcripts[str(call_id)] = {
        "transcript": transcript,
        "record_url": record_url,
        "whisper_model": whisper_model,
        "audio_url": audio_url,
        "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    save_transcripts_cache()


persisted_transcripts: Dict[str, dict] = load_transcripts_cache()
persisted_schedules: Dict[str, dict] = load_schedules()
persisted_queue: Dict[str, dict] = load_queue()
scheduler_task: Optional[asyncio.Task] = None
running_schedule_ids = set()
queue_runner_task: Optional[asyncio.Task] = None
queue_worker_tasks: List[asyncio.Task] = []
queue_runner_start_lock = asyncio.Lock()
queue_runner_stop_requested = False
queue_poll_interval_seconds = 0.2

QUEUE_STATUSES = {"queued", "processing", "done", "failed", "skipped", "canceled"}


def now_utc_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def build_queue_counters() -> dict:
    counters = {
        "total": len(persisted_queue),
        "queued": 0,
        "processing": 0,
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "canceled": 0,
    }
    for item in persisted_queue.values():
        status = item.get("status")
        if status in counters:
            counters[status] += 1
    return counters


def normalize_queue_item(record: dict, api_host: str, domain: str, access_token: str, whisper_model: Optional[str]) -> dict:
    call_id = str(record.get("id") or record.get("call_id") or "").strip()
    if not call_id:
        raise ValueError("record.id обязателен")

    status = str(record.get("status") or "queued")
    if status not in QUEUE_STATUSES:
        status = "queued"

    queue_item = {
        "call_id": call_id,
        "datetime_start": record.get("datetime_start"),
        "caller_a": record.get("caller_a") or record.get("phone_a") or "",
        "caller_b": record.get("caller_b") or record.get("phone_b") or "",
        "duration": int(record.get("duration") or 0),
        "scenario_name": record.get("scenario_name") or (record.get("scenario") or {}).get("title") or (record.get("scenario") or {}).get("name") or "",
        "record_url": record.get("record_url"),
        "timezone": extract_call_timezone(record),
        "status": status,
        "error_message": record.get("error_message"),
        "transcript_text": record.get("transcript_text") or record.get("transcript") or "",
        "audio_url": record.get("audio_url") or record.get("local_audio_url"),
        "created_at": record.get("created_at") or now_utc_iso(),
        "updated_at": now_utc_iso(),
        "api_host": (api_host or "").strip(),
        "domain": (domain or "").strip(),
        "access_token": (access_token or "").strip(),
        "whisper_model": normalize_whisper_model_name(whisper_model),
    }
    return queue_item


def queue_item_to_public(item: dict) -> dict:
    public_keys = [
        "call_id",
        "datetime_start",
        "caller_a",
        "caller_b",
        "duration",
        "scenario_name",
        "status",
        "error_message",
        "transcript_text",
        "audio_url",
        "created_at",
        "updated_at",
    ]
    return {key: item.get(key) for key in public_keys}


def queue_item_to_record(item: dict) -> dict:
    return {
        "id": str(item.get("call_id") or ""),
        "datetime_start": item.get("datetime_start"),
        "timezone": item.get("timezone"),
        "phone_a": item.get("caller_a"),
        "phone_b": item.get("caller_b"),
        "duration": item.get("duration"),
        "record_url": item.get("record_url"),
        "scenario_name": item.get("scenario_name"),
        "transcript": item.get("transcript_text") or "",
        "local_audio_url": item.get("audio_url"),
    }


async def send_ws_broadcast(payload: dict) -> None:
    if not active_connections:
        return
    for session_id in list(active_connections.keys()):
        await send_ws_message(session_id, payload)


async def emit_queue_snapshot() -> None:
    items = sorted(
        [queue_item_to_public(item) for item in persisted_queue.values()],
        key=lambda row: row.get("created_at") or "",
        reverse=True,
    )
    await send_ws_broadcast({
        "type": "queue_snapshot",
        "items": items,
        "counters": build_queue_counters(),
        "runner": {
            "running": bool(queue_runner_task and not queue_runner_task.done()),
            "stop_requested": queue_runner_stop_requested,
            "active_workers": len([task for task in queue_worker_tasks if not task.done()]),
        },
    })


def get_queued_call_ids_sorted() -> List[str]:
    queued_items = [item for item in persisted_queue.values() if item.get("status") == "queued"]
    queued_items.sort(key=lambda row: row.get("created_at") or "")
    return [str(item.get("call_id") or "") for item in queued_items if item.get("call_id")]


async def process_queue_item(item: dict) -> None:
    call_id = str(item.get("call_id") or "")
    if not call_id:
        return

    record_url = item.get("record_url")
    if not record_url:
        item["status"] = "failed"
        item["error_message"] = "У звонка нет record_url"
        item["updated_at"] = now_utc_iso()
        save_queue()
        await emit_queue_snapshot()
        return

    item["status"] = "processing"
    item["error_message"] = None
    item["updated_at"] = now_utc_iso()
    save_queue()
    await emit_queue_snapshot()

    cached = get_cached_transcript(call_id, record_url=record_url)
    if cached and cached.get("transcript"):
        item["status"] = "done"
        item["transcript_text"] = cached.get("transcript") or ""
        item["audio_url"] = cached.get("audio_url")
        item["updated_at"] = now_utc_iso()
        save_queue()
        await send_ws_broadcast({"type": "record", "record": queue_item_to_record(item)})
        await emit_queue_snapshot()
        return

    try:
        result = await transcribe_call({
            "id": call_id,
            "record_url": record_url,
            "whisper_model": item.get("whisper_model") or DEFAULT_WHISPER_MODEL,
            "access_token": item.get("access_token"),
            "api_host": item.get("api_host"),
            "domain": item.get("domain"),
        })
        if result.get("skipped"):
            item["status"] = "skipped"
            item["error_message"] = result.get("error") or "Транскрибация пропущена из-за некорректного аудио"
            item["transcript_text"] = ""
            item["updated_at"] = now_utc_iso()
            save_queue()
            await emit_queue_snapshot()
            return

        item["status"] = "done"
        item["error_message"] = None
        item["transcript_text"] = result.get("transcript") or ""
        item["audio_url"] = result.get("audio_url")
        item["updated_at"] = now_utc_iso()
        save_queue()
        await send_ws_broadcast({"type": "record", "record": queue_item_to_record(item)})
        await emit_queue_snapshot()
    except Exception as exc:
        item["status"] = "failed"
        item["error_message"] = str(exc)
        item["updated_at"] = now_utc_iso()
        save_queue()
        await emit_queue_snapshot()


async def queue_worker(worker_id: int, work_queue: asyncio.Queue, enqueued_ids: set) -> None:
    while True:
        try:
            call_id = await asyncio.wait_for(work_queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            if queue_runner_stop_requested and work_queue.empty():
                break
            continue

        try:
            if call_id is None:
                break

            item = persisted_queue.get(str(call_id))
            if not item or item.get("status") != "queued":
                continue

            await process_queue_item(item)
        except Exception as exc:
            logger.error(f"[QUEUE] Worker {worker_id} failed for call_id={call_id}: {exc}", exc_info=True)
            if call_id is not None:
                failed_item = persisted_queue.get(str(call_id))
                if failed_item and failed_item.get("status") in {"queued", "processing"}:
                    failed_item["status"] = "failed"
                    failed_item["error_message"] = str(exc)
                    failed_item["updated_at"] = now_utc_iso()
                    save_queue()
                    await emit_queue_snapshot()
        finally:
            if call_id is not None:
                enqueued_ids.discard(str(call_id))
            work_queue.task_done()


async def queue_runner_loop() -> None:
    global queue_runner_task, queue_runner_stop_requested, queue_worker_tasks
    logger.info("[QUEUE] Queue runner started")
    await send_ws_broadcast({"type": "status", "message": "[QUEUE] Запущена обработка очереди транскрибации."})
    await emit_queue_snapshot()

    worker_count = max(1, runtime_performance["queue_workers"])
    work_queue: asyncio.Queue = asyncio.Queue()
    enqueued_ids: set = set()
    queue_worker_tasks = [
        asyncio.create_task(queue_worker(index + 1, work_queue, enqueued_ids))
        for index in range(worker_count)
    ]

    logger.info(f"[QUEUE] Queue worker pool started with {worker_count} workers")

    try:
        while True:
            if queue_runner_stop_requested:
                logger.info("[QUEUE] Stop requested, runner will stop after draining current worker tasks")
                break

            for call_id in get_queued_call_ids_sorted():
                if call_id in enqueued_ids:
                    continue
                enqueued_ids.add(call_id)
                await work_queue.put(call_id)

            counters = build_queue_counters()
            if counters["queued"] == 0 and counters["processing"] == 0 and work_queue.empty():
                break

            await asyncio.sleep(queue_poll_interval_seconds)

        for _ in range(worker_count):
            await work_queue.put(None)

        await asyncio.gather(*queue_worker_tasks, return_exceptions=True)
    except Exception as exc:
        logger.error(f"[QUEUE] Queue runner failed: {exc}", exc_info=True)
        await send_ws_broadcast({"type": "status", "message": f"[QUEUE] Ошибка обработки очереди: {exc}"})
    finally:
        for worker_task in queue_worker_tasks:
            if not worker_task.done():
                worker_task.cancel()
        if queue_worker_tasks:
            await asyncio.gather(*queue_worker_tasks, return_exceptions=True)
        queue_worker_tasks = []
        queue_runner_task = None
        stop_was_requested = queue_runner_stop_requested
        queue_runner_stop_requested = False
        await emit_queue_snapshot()
        if stop_was_requested:
            await send_ws_broadcast({"type": "status", "message": "[QUEUE] Обработка очереди остановлена пользователем."})
        else:
            await send_ws_broadcast({"type": "status", "message": "[QUEUE] Обработка очереди завершена."})
        logger.info("[QUEUE] Queue runner finished")


def sanitize_weekdays(raw_weekdays: List[int]) -> List[int]:
    result = sorted(set(int(day) for day in raw_weekdays if 0 <= int(day) <= 6))
    return result


def is_hour_in_window(hour: int, from_hour: int, to_hour: int) -> bool:
    if from_hour <= to_hour:
        return from_hour <= hour <= to_hour
    return hour >= from_hour or hour <= to_hour


def get_schedule_slot_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d-%H")


def get_dynamic_page_budget(records_limit: int) -> int:
    """Estimate how many source pages may be needed to collect filtered records.

    We intentionally overshoot the exact math because filters (duration/recording/scenario)
    can discard many entries from each fetched page.
    """
    safe_limit = max(1, int(records_limit or 1))
    estimated = max(10, (safe_limit // max(1, SEARCH_PAGE_SIZE)) * 5 + 10)
    return min(MAX_SEARCH_PAGES_HARD_LIMIT, estimated)


def is_schedule_due(schedule: dict, now: datetime) -> bool:
    if not schedule.get("enabled", True):
        return False

    weekdays = schedule.get("weekdays") or []
    if now.weekday() not in weekdays:
        return False

    from_hour = int(schedule.get("from_hour", 0))
    to_hour = int(schedule.get("to_hour", 23))
    if not is_hour_in_window(now.hour, from_hour, to_hour):
        return False

    slot_key = get_schedule_slot_key(now)
    if schedule.get("last_run_slot") == slot_key:
        return False

    return True


def normalize_datetime_for_api(value: str) -> str:
    if "T" in value:
        value = value.replace("T", " ")
    if len(value) == 16:
        value += ":00"
    return value


def parse_input_datetime(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(normalize_datetime_for_api(value))
    except Exception:
        return None


def clamp_datetime_range_to_now(from_date: str, to_date: str) -> tuple[str, str, bool]:
    from_value = normalize_datetime_for_api(from_date)
    to_value = normalize_datetime_for_api(to_date)

    now_value = datetime.now().replace(microsecond=0)
    from_dt = parse_input_datetime(from_value)
    to_dt = parse_input_datetime(to_value)
    to_was_clamped = False

    # Protect API calls from future timestamps in user-selected range.
    if to_dt and to_dt > now_value:
        to_dt = now_value
        to_value = to_dt.strftime("%Y-%m-%d %H:%M:%S")
        to_was_clamped = True

    if from_dt and from_dt > now_value:
        from_dt = now_value
        from_value = from_dt.strftime("%Y-%m-%d %H:%M:%S")

    if from_dt and to_dt and from_dt > to_dt:
        from_value = to_dt.strftime("%Y-%m-%d %H:%M:%S")

    return from_value, to_value, to_was_clamped


def parse_call_datetime_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        # Voximplant may return either "YYYY-mm-dd HH:MM:SS" or ISO-like strings.
        normalized = value.replace("T", " ").replace("Z", "")
        dt = datetime.fromisoformat(normalized)
        return dt
    except Exception:
        return None


def extract_call_timezone(payload: dict) -> Optional[str]:
    timezone_keys = [
        "timezone",
        "time_zone",
        "tz",
        "timezone_name",
        "datetime_start_timezone",
    ]
    for key in timezone_keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def fetch_calls_for_schedule_window(schedule: dict, from_dt: datetime, to_dt: datetime) -> List[dict]:
    api_host, account_name = resolve_api_host_and_account(schedule.get("api_host", ""), schedule.get("domain", ""))
    access_token = schedule.get("access_token", "")
    if not api_host or not account_name or not access_token:
        raise RuntimeError("Некорректные credentials в расписании")

    scenario_id = schedule.get("scenario_id")
    min_duration = int(schedule.get("min_duration", 0) or 0)
    has_recording = bool(schedule.get("has_recording", True))
    records_limit = int(schedule.get("records_limit", 100) or 100)

    api_url = f"https://{api_host}/api/v4/history/searchCalls?domain={account_name}"
    from_value = normalize_datetime_for_api(from_dt.strftime("%Y-%m-%d %H:%M:%S"))
    to_value = normalize_datetime_for_api(to_dt.strftime("%Y-%m-%d %H:%M:%S"))

    cursor = None
    page_number = 0
    calls = []

    async with httpx.AsyncClient(timeout=120) as client:
        while page_number < MAX_SEARCH_PAGES and len(calls) < records_limit:
            page_number += 1
            form_data = {
                "access_token": access_token,
                "from": from_value,
                "to": to_value,
                "with_scenarios": "true",
                "limit": SEARCH_PAGE_SIZE,
            }
            if scenario_id:
                form_data["scenario_ids"] = f"[{scenario_id}]"
            if cursor:
                form_data["cursor"] = cursor

            try:
                payload = await fetch_voximplant_page(client, api_url, form_data)
            except httpx.HTTPStatusError as exc:
                details = None
                try:
                    details = exc.response.text
                except Exception:
                    details = str(exc)
                raise RuntimeError(
                    f"Ошибка Voximplant при загрузке звонков по расписанию: "
                    f"{exc.response.status_code} {exc.response.reason_phrase} — {details}"
                ) from exc
            results = payload.get("result") or []
            if not isinstance(results, list):
                results = []

            for item in results:
                if len(calls) >= records_limit:
                    break

                record_url = item.get("record_url")
                duration = int(item.get("duration") or 0)
                if has_recording and not record_url:
                    continue
                if min_duration and duration < min_duration:
                    continue

                call_dt = parse_call_datetime_utc(item.get("datetime_start"))
                # Safety filter: keep calls strictly by recording timestamp window.
                if call_dt and (call_dt < from_dt or call_dt > to_dt):
                    continue

                call_id = str(item.get("id"))

                calls.append({
                    "id": call_id,
                    "record_url": record_url,
                    "datetime_start": item.get("datetime_start"),
                    "phone_a": item.get("phone_a"),
                    "phone_b": item.get("phone_b"),
                    "duration": duration,
                })

            meta = payload.get("_meta") or {}
            cursor = meta.get("cursor")
            if not cursor:
                break
            await asyncio.sleep(SEARCH_RETRY_DELAY_SECONDS)

    return calls


async def run_schedule_for_current_hour(schedule_id: str, slot_key: str) -> None:
    schedule = persisted_schedules.get(schedule_id)
    if not schedule:
        return

    if schedule_id in running_schedule_ids:
        return

    running_schedule_ids.add(schedule_id)
    try:
        # Use UTC hour slot so schedule is based on call recording timestamps, not local server timezone.
        now_utc = datetime.utcnow().replace(microsecond=0)
        window_start = now_utc.replace(minute=0, second=0, microsecond=0)
        hour_end = window_start + timedelta(hours=1) - timedelta(seconds=1)
        window_end = min(now_utc, hour_end)
        if window_start > window_end:
            window_start = window_end - timedelta(minutes=1)

        calls = await fetch_calls_for_schedule_window(schedule, window_start, window_end)
        transcribed_count = 0
        cached_count = 0
        failed_count = 0

        whisper_model = normalize_whisper_model_name(schedule.get("whisper_model"))
        for call in calls:
            record_url = call.get("record_url")
            if not record_url:
                continue

            try:
                result = await transcribe_call({
                    "id": call.get("id"),
                    "record_url": record_url,
                    "whisper_model": whisper_model,
                    "access_token": schedule.get("access_token"),
                    "api_host": schedule.get("api_host"),
                    "domain": schedule.get("domain"),
                })
                if result.get("cached"):
                    cached_count += 1
                else:
                    transcribed_count += 1
            except Exception as exc:
                failed_count += 1
                logger.error(f"[SCHEDULER] Ошибка транскрибации в расписании {schedule_id}: {exc}")

        persisted_schedules[schedule_id]["last_run_slot"] = slot_key
        persisted_schedules[schedule_id]["last_run_at"] = datetime.now().isoformat(timespec="seconds")
        persisted_schedules[schedule_id]["last_result"] = {
            "calls_found": len(calls),
            "transcribed": transcribed_count,
            "cached": cached_count,
            "failed": failed_count,
            "window_start": window_start.isoformat(timespec="seconds"),
            "window_end": window_end.isoformat(timespec="seconds"),
        }
        save_schedules()
        logger.info(
            f"[SCHEDULER] Расписание {schedule_id}: calls={len(calls)}, transcribed={transcribed_count}, cached={cached_count}, failed={failed_count}"
        )
    except Exception as exc:
        logger.error(f"[SCHEDULER] Выполнение расписания {schedule_id} завершилось ошибкой: {exc}", exc_info=True)
    finally:
        running_schedule_ids.discard(schedule_id)


async def scheduler_loop() -> None:
    logger.info("[SCHEDULER] Фоновый планировщик запущен")
    while True:
        try:
            now = datetime.utcnow()
            for schedule_id, schedule in list(persisted_schedules.items()):
                if is_schedule_due(schedule, now):
                    slot_key = get_schedule_slot_key(now)
                    asyncio.create_task(run_schedule_for_current_hour(schedule_id, slot_key))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[SCHEDULER] Ошибка цикла планировщика: {exc}", exc_info=True)
        await asyncio.sleep(SCHEDULER_POLL_SECONDS)


@app.on_event("startup")
async def startup_scheduler() -> None:
    global scheduler_task
    configure_torch_threads()
    if WHISPER_INFERENCE_ISOLATION == "concurrent":
        logger.warning(
            "[WHISPER] Режим concurrent для одной модели на CPU может вызывать деградацию/зависания. "
            "Рекомендуется serialized."
        )
    if scheduler_task is None or scheduler_task.done():
        scheduler_task = asyncio.create_task(scheduler_loop())


@app.on_event("shutdown")
async def shutdown_scheduler() -> None:
    global scheduler_task, queue_runner_task, queue_worker_tasks, queue_runner_stop_requested
    if scheduler_task and not scheduler_task.done():
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
    scheduler_task = None

    queue_runner_stop_requested = True
    if queue_runner_task and not queue_runner_task.done():
        queue_runner_task.cancel()
        try:
            await queue_runner_task
        except asyncio.CancelledError:
            pass
    queue_runner_task = None

    for task in queue_worker_tasks:
        if not task.done():
            task.cancel()
    queue_worker_tasks = []


async def get_or_load_whisper_model(model_name: str):
    normalized_name = normalize_whisper_model_name(model_name)
    if normalized_name in loaded_models:
        return loaded_models[normalized_name]

    existing_task = loading_tasks.get(normalized_name)
    if existing_task is not None:
        logger.info(f"[WHISPER] Ожидаем завершения загрузки модели: {normalized_name}")
        return await existing_task

    if normalized_name not in model_states:
        model_states[normalized_name] = {"status": "loading", "error": None}

    logger.info(f"[WHISPER] Подготавливаем модель: {normalized_name}")

    async def _load_model() -> object:
        try:
            model = await asyncio.to_thread(
                whisper.load_model,
                normalized_name,
                device="cpu",
                download_root=str(MODELS_DIR),
            )
            loaded_models[normalized_name] = model
            model_states[normalized_name] = {"status": "ready", "progress": 100, "error": None}
            logger.info(f"[WHISPER] Модель готова: {normalized_name}")
            return model
        except Exception as exc:
            model_states[normalized_name] = {"status": "error", "error": str(exc)}
            logger.error(f"[WHISPER] Ошибка загрузки модели {normalized_name}: {exc}", exc_info=True)
            raise
        finally:
            loading_tasks.pop(normalized_name, None)

    task = asyncio.create_task(_load_model())
    loading_tasks[normalized_name] = task
    return await task


async def emit_model_progress(model_name: str, job_id: str) -> None:
    for percent in [10, 25, 45, 70, 90, 100]:
        if model_states.get(model_name, {}).get("status") != "loading":
            break
        model_states[model_name] = {
            **model_states.get(model_name, {}),
            "status": "loading",
            "progress": percent,
            "job_id": job_id,
        }
        logger.info(f"[WHISPER] Загрузка модели {model_name}: {percent}%")
        if percent < 100:
            await asyncio.sleep(1)


async def transcribe_audio_with_whisper(model, audio_path: Path, whisper_model: str):
    def make_skipped_result(reason: str) -> dict:
        return {
            "text": "",
            "language": "unknown",
            "segments": [],
            "skipped": True,
            "error": reason,
        }

    def detect_invalid_audio_reason(path: Path) -> Optional[str]:
        try:
            file_size = os.path.getsize(path)
        except OSError as exc:
            return f"Не удалось прочитать размер файла: {exc}"

        if file_size < MIN_AUDIO_FILE_BYTES:
            return f"Аудиофайл слишком маленький ({file_size} bytes)"

        try:
            audio = whisper.load_audio(str(path))
        except Exception as exc:
            return f"Whisper не смог декодировать аудио: {exc}"

        if audio is None or audio.size == 0:
            return "Whisper load_audio вернул пустой массив"

        duration_seconds = float(audio.shape[0]) / 16000.0
        if duration_seconds < MIN_AUDIO_SECONDS_FOR_TRANSCRIBE:
            return f"Аудио слишком короткое ({duration_seconds:.3f}s)"

        return None

    transcribe_kwargs = {
        "task": "transcribe",
        "fp16": False,
        "word_timestamps": False,
        "verbose": False,
    }
    if is_multilingual_whisper_model(whisper_model):
        transcribe_kwargs["language"] = None
        logger.info("[TRANSCRIBE] Используем multilingual-режим для автоопределения языка")
    else:
        transcribe_kwargs["language"] = "en"

    def run_transcribe_sync(path: Path, kwargs: dict):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                return model.transcribe(str(path), **kwargs)
            except RuntimeError as exc:
                message = str(exc)
                if "tensor of 0 elements" in message:
                    logger.warning(
                        f"[TRANSCRIBE] Whisper вернул zero-tensor для {path.name}. "
                        f"Файл будет пропущен: {message}"
                    )
                    return make_skipped_result("Пустой/поврежденный аудиофайл (zero-tensor)")
                raise

    async def run_transcribe(path: Path, kwargs: dict, timeout_seconds: int):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(run_transcribe_sync, path, kwargs),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"Таймаут транскрибации ({timeout_seconds} сек) для файла {path.name}"
            ) from exc

    invalid_audio_reason = await asyncio.to_thread(detect_invalid_audio_reason, audio_path)
    if invalid_audio_reason:
        logger.warning(f"[TRANSCRIBE] Пропуск аудио {audio_path.name}: {invalid_audio_reason}")
        result = make_skipped_result(invalid_audio_reason)
        return result, ""

    try:
        result = await run_transcribe(audio_path, transcribe_kwargs, WHISPER_TRANSCRIBE_TIMEOUT_SECONDS)
    except RuntimeError as exc:
        # Whisper can fail on very short/degenerate segments with zero-length attention tensors.
        if "cannot reshape tensor of 0 elements" in str(exc):
            logger.warning("[TRANSCRIBE] Обнаружен нулевой тензор, пробую повтор с padded-аудио")
            try:
                padded_path = await asyncio.to_thread(ensure_min_audio_duration, audio_path)
                result = await run_transcribe(padded_path, transcribe_kwargs, WHISPER_TRANSCRIBE_TIMEOUT_SECONDS)
            except RuntimeError as padded_exc:
                if "tensor of 0 elements" in str(padded_exc):
                    logger.warning(
                        f"[TRANSCRIBE] even after padding audio {audio_path.name} is invalid: {padded_exc}"
                    )
                    result = make_skipped_result("Пустой/поврежденный аудиофайл после fallback-padding")
                else:
                    raise
        else:
            raise

    transcript = str(result.get("text") or "").strip()

    if result.get("skipped"):
        return result, ""

    if not transcript:
        logger.warning("[TRANSCRIBE] Текст пустой, пробую повторную транскрибацию с безопасными параметрами")
        fallback_kwargs = {
            **transcribe_kwargs,
            "temperature": 0.0,
            "condition_on_previous_text": False,
        }
        fallback_kwargs.pop("word_timestamps", None)
        try:
            result = await run_transcribe(audio_path, fallback_kwargs, WHISPER_FALLBACK_TIMEOUT_SECONDS)
            transcript = str(result.get("text") or "").strip()
        except RuntimeError as fallback_exc:
            if "tensor of 0 elements" in str(fallback_exc):
                logger.warning(
                    f"[TRANSCRIBE] fallback вернул zero-tensor для {audio_path.name}: {fallback_exc}"
                )
                result = make_skipped_result("Fallback получил zero-tensor")
                transcript = ""
            else:
                logger.warning(f"[TRANSCRIBE] Повторная транскрибация не удалась: {fallback_exc}")
        except Exception as fallback_exc:
            logger.warning(f"[TRANSCRIBE] Повторная транскрибация не удалась: {fallback_exc}")

    return result, transcript


async def download_whisper_model_background(model_name: str, job_id: str) -> None:
    normalized_name = normalize_whisper_model_name(model_name)
    model_states[normalized_name] = {"status": "loading", "progress": 0, "job_id": job_id, "error": None}
    logger.info(f"[WHISPER] Начинаю фоновой загрузку модели: {normalized_name}")
    progress_task = asyncio.create_task(emit_model_progress(normalized_name, job_id))
    try:
        await get_or_load_whisper_model(normalized_name)
        model_states[normalized_name] = {"status": "ready", "progress": 100, "job_id": job_id, "error": None}
        logger.info(f"[WHISPER] Фоновая загрузка завершена: {normalized_name}")
    except Exception as exc:
        model_states[normalized_name] = {"status": "error", "progress": 0, "job_id": job_id, "error": str(exc)}
        logger.error(f"[WHISPER] Фоновая загрузка не удалась: {normalized_name}: {exc}", exc_info=True)
    finally:
        if not progress_task.done():
            progress_task.cancel()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    session_id = websocket.query_params.get("session_id")
    if not session_id:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    active_connections[session_id] = websocket
    active_sessions[session_id] = {"calls": [], "transcripts": {}}
    await send_ws_message(session_id, {
        "type": "queue_snapshot",
        "items": sorted(
            [queue_item_to_public(item) for item in persisted_queue.values()],
            key=lambda row: row.get("created_at") or "",
            reverse=True,
        ),
        "counters": build_queue_counters(),
        "runner": {
            "running": bool(queue_runner_task and not queue_runner_task.done()),
            "stop_requested": queue_runner_stop_requested,
            "active_workers": len([task for task in queue_worker_tasks if not task.done()]),
        },
    })

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections.pop(session_id, None)
        active_sessions.pop(session_id, None)


async def send_ws_message(session_id: str, payload: dict) -> None:
    websocket = active_connections.get(session_id)
    if websocket is None:
        return

    try:
        await websocket.send_json(payload)
    except Exception:
        active_connections.pop(session_id, None)


@app.post("/api/process")
async def process_calls(request: ProcessRequest, background_tasks: BackgroundTasks):
    if not request.session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    if request.session_id not in active_connections:
        raise HTTPException(
            status_code=400,
            detail="WebSocket не подключен. Сначала откройте /ws?session_id=...",
        )

    active_sessions[request.session_id] = {"calls": [], "transcripts": {}}
    background_tasks.add_task(run_processing, request)
    return {"detail": "Обработка запущена"}


@app.post("/api/audio/download")
async def download_audio_for_playback(body: dict):
    call_id = None if body.get("id") is None else str(body.get("id"))
    record_url = body.get("record_url")
    access_token = body.get("access_token")
    api_host = body.get("api_host")
    domain = body.get("domain")

    if not record_url:
        raise HTTPException(status_code=400, detail="record_url обязателен")

    filename = get_safe_filename(record_url)
    audio_path = AUDIO_DIR / filename
    try:
        if audio_path.exists() and audio_path.stat().st_size > 0:
            audio_url = build_audio_url(audio_path)
            return {"id": call_id, "audio_url": audio_url, "filename": audio_path.name, "cached": True}

        await download_audio(record_url, audio_path, access_token=access_token, api_host=api_host, domain=domain)
        audio_url = build_audio_url(audio_path)
        return {"id": call_id, "audio_url": audio_url, "filename": audio_path.name, "cached": False}
    except Exception as exc:
        logger.error(f"[AUDIO] Ошибка подготовки аудио для playback: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка подготовки аудио: {str(exc)}")


@app.post("/api/transcribe")
async def transcribe_call(body: dict):
    global active_transcriptions, active_inference_tasks
    call_id = None if body.get("id") is None else str(body.get("id"))
    record_url = body.get("record_url")
    whisper_model = body.get("whisper_model") or DEFAULT_WHISPER_MODEL
    access_token = body.get("access_token")
    api_host = body.get("api_host")
    domain = body.get("domain")
    whisper_model = normalize_whisper_model_name(whisper_model)

    logger.info(f"[TRANSCRIBE] Запрос: call_id={call_id}")
    logger.info(f"[TRANSCRIBE] Используемая модель Whisper: {whisper_model}")

    if not call_id or not record_url:
        logger.error(f"[TRANSCRIBE] Ошибка: отсутствуют обязательные параметры")
        raise HTTPException(status_code=400, detail="id и record_url обязательны")

    cached = get_cached_transcript(call_id, record_url=record_url)
    if cached and cached.get("transcript"):
        logger.info(f"[TRANSCRIBE] Использую кэшированный результат для звонка {call_id}")
        return {
            "id": call_id,
            "transcript": cached.get("transcript", ""),
            "audio_url": cached.get("audio_url"),
            "cached": True,
        }

    async with transcribe_limiter.slot():
        logger.info(
            f"[TRANSCRIBE] Ожидаю доступного слота транскрибации "
            f"({runtime_performance['max_concurrency']} параллельных задач)..."
        )
        active_transcriptions += 1
        filename = get_safe_filename(record_url)
        audio_path = AUDIO_DIR / filename
        try:
            logger.info(f"[TRANSCRIBE] Загрузка аудио: {record_url} -> {audio_path}")
            await download_audio(record_url, audio_path, access_token=access_token, api_host=api_host, domain=domain)
            file_size = audio_path.stat().st_size
            logger.info(f"[TRANSCRIBE] Аудио успешно загружено ({file_size} bytes)")

            if file_size == 0:
                logger.error(f"[TRANSCRIBE] Ошибка: аудиофайл пустой (0 bytes)")
                raise Exception("Скачанный аудиофайл пустой")

            logger.info(f"[TRANSCRIBE] Нормализация аудио...")
            audio_path = await asyncio.to_thread(normalize_audio, audio_path)
            audio_url = build_audio_url(audio_path)

            logger.info(f"[TRANSCRIBE] Загрузка модели Whisper ({whisper_model}, CPU)...")
            model = await get_or_load_whisper_model(whisper_model)
            logger.info(
                f"[TRANSCRIBE] Модель загружена, режим inference={WHISPER_INFERENCE_ISOLATION}"
            )

            async with maybe_model_inference_lock(whisper_model):
                active_inference_tasks += 1
                try:
                    logger.info(f"[TRANSCRIBE] Начинаем inference на модели {whisper_model}")
                    result, transcript = await transcribe_audio_with_whisper(model, audio_path, whisper_model)
                finally:
                    active_inference_tasks = max(0, active_inference_tasks - 1)

            logger.info(f"[TRANSCRIBE] Полный результат модели: {result}")
            logger.info(f"[TRANSCRIBE] Детектированный язык: {result.get('language', 'unknown')}")
            logger.info(f"[TRANSCRIBE] Доступные поля: {list(result.keys())}")

            logger.info(f"[TRANSCRIBE] Извлеченный текст: '{transcript}'")
            logger.info(f"[TRANSCRIBE] OK - Успешно! Длина текста: {len(transcript)} символов")
            skipped = bool(result.get("skipped"))
            if skipped:
                logger.warning(
                    f"[TRANSCRIBE] Звонок {call_id} пропущен: {result.get('error') or 'некорректное аудио'}"
                )
            else:
                upsert_cached_transcript(
                    call_id=call_id,
                    transcript=transcript,
                    record_url=record_url,
                    whisper_model=whisper_model,
                    audio_url=audio_url,
                )
        except Exception as exc:
            logger.error(f"[TRANSCRIBE] ERROR: {str(exc)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Ошибка транскрибации: {str(exc)}")
        finally:
            active_transcriptions = max(0, active_transcriptions - 1)

    return {
        "id": call_id,
        "transcript": transcript,
        "audio_url": audio_url,
        "cached": False,
        "skipped": bool(result.get("skipped")),
        "error": result.get("error"),
    }


@app.post("/api/transcripts/export-xlsx")
async def export_transcripts_xlsx(body: dict):
    records = body.get("records") or []
    transcribed = [r for r in records if (r.get("transcript") or "").strip()]

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Transcripts"
    sheet.append(["Дата", "caller_a", "caller_b", "Транскрипт"])

    for rec in transcribed:
        dt_raw = rec.get("datetime_start") or ""
        try:
            dt = datetime.fromisoformat(dt_raw).strftime("%d.%m.%Y %H:%M:%S")
        except Exception:
            dt = dt_raw
        sheet.append([
            dt,
            rec.get("phone_a") or rec.get("caller_a") or "",
            rec.get("phone_b") or rec.get("caller_b") or "",
            (rec.get("transcript") or "").strip(),
        ])

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)

    filename = f"transcripts-{datetime.now().strftime('%Y%m%d-%H%M%S')}.xlsx"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@app.get("/api/whisper/models")
async def get_whisper_models():
    models = []
    for model_name in SUPPORTED_WHISPER_MODELS:
        state = get_model_status(model_name)
        status = state.get("status", "missing")
        if status == "ready":
            status_label = "готово"
        elif status == "loading":
            status_label = f"загрузка {state.get('progress', 0)}%"
        elif status == "error":
            status_label = "ошибка"
        else:
            status_label = "не скачана"

        models.append({
            "name": model_name,
            "status": status,
            "status_label": status_label,
            "progress": state.get("progress", 0),
            "error": state.get("error"),
            "cache_path": state.get("cache_path"),
        })
    return {"models": models, "default_model": DEFAULT_WHISPER_MODEL}


@app.get("/api/performance/diagnostics")
async def get_performance_diagnostics():
    available_slots = transcribe_limiter.available
    return {
        "cpu": {
            "logical_cores": LOGICAL_CPU_COUNT,
            "physical_cores": PHYSICAL_CPU_COUNT,
            "usable_cores": runtime_performance["usable_cores"],
            "reserved_cores": runtime_performance["reserved_cores"],
        },
        "performance_profile": {
            "active_profile": runtime_performance["profile"],
            "supported": sorted(list(SUPPORTED_PERFORMANCE_PROFILES)),
        },
        "transcription": {
            "max_concurrency": runtime_performance["max_concurrency"],
            "active": active_transcriptions,
            "active_inference": active_inference_tasks,
            "available_slots": available_slots,
            "model_inference_mode": (
                "serialized-per-model-instance" if WHISPER_INFERENCE_ISOLATION == "serialized" else "concurrent-readonly"
            ),
            "queue_workers": runtime_performance["queue_workers"],
        },
        "torch": {
            "num_threads": runtime_performance["torch_num_threads"],
            "interop_threads": runtime_performance["torch_num_interop_threads"],
            "configured": TORCH_THREADS_CONFIGURED,
            "pending_update": False,
        },
        "timeouts": {
            "primary_seconds": WHISPER_TRANSCRIBE_TIMEOUT_SECONDS,
            "fallback_seconds": WHISPER_FALLBACK_TIMEOUT_SECONDS,
        },
    }


@app.post("/api/performance/profile")
async def set_performance_profile(payload: PerformanceProfileRequest):
    result = await apply_performance_profile(payload.profile)
    await emit_queue_snapshot()
    return {
        "status": "updated",
        **result,
    }


@app.post("/api/whisper/download-model")
async def download_whisper_model(payload: dict):
    model_name = normalize_whisper_model_name(payload.get("model_name"))
    job_id = str(uuid.uuid4())
    asyncio.create_task(download_whisper_model_background(model_name, job_id))
    return {"status": "queued", "model_name": model_name, "job_id": job_id}


@app.get("/api/scenarios")
async def get_scenarios(api_host: Optional[str] = "", domain: Optional[str] = "", access_token: Optional[str] = ""):
    api_host, account_name = resolve_api_host_and_account(api_host, domain)

    if not api_host:
        raise HTTPException(status_code=400, detail="Не указан хост API (host).")
    if not account_name:
        raise HTTPException(status_code=400, detail="Не указан домен аккаунта (domain).")
    if not access_token:
        raise HTTPException(status_code=400, detail="Не указан access_token.")

    all_results = []
    page = 1
    per_page = 100
    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            url = f"https://{api_host}/api/v3/scenario/searchScenarios?domain={account_name}&sort=-id"
            form_data = {
                "access_token": access_token,
                "page": page,
                "per-page": per_page,
            }
            try:
                response = await client.post(
                    url,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data=form_data,
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Ошибка поиска сценариев: {str(exc)}")

            results = payload.get("result") or []
            if not isinstance(results, list):
                results = []

            all_results.extend(results)
            meta = payload.get("_meta") or {}
            page_count = int(meta.get("pageCount") or 1)

            if page >= page_count:
                break
            page += 1

    unique_results = []
    seen_ids = set()
    for item in all_results:
        item_id = item.get("id")
        if item_id not in seen_ids:
            seen_ids.add(item_id)
            unique_results.append(item)

    return [{"id": item.get("id"), "title": item.get("title")} for item in unique_results]


@app.get("/api/schedules")
async def list_schedules():
    return {
        "schedules": sorted(
            [
                {"id": schedule_id, **schedule}
                for schedule_id, schedule in persisted_schedules.items()
            ],
            key=lambda item: item.get("created_at", ""),
            reverse=True,
        )
    }


@app.post("/api/schedules")
async def create_schedule(payload: ScheduleRequest):
    weekdays = sanitize_weekdays(payload.weekdays)
    if not weekdays:
        raise HTTPException(status_code=400, detail="Нужно выбрать хотя бы один день недели")
    if not (0 <= payload.from_hour <= 23 and 0 <= payload.to_hour <= 23):
        raise HTTPException(status_code=400, detail="Часы должны быть в диапазоне 0..23")
    if not payload.api_host or not payload.domain or not payload.access_token:
        raise HTTPException(status_code=400, detail="Для расписания нужны host, domain и access_token")

    schedule_id = str(uuid.uuid4())
    persisted_schedules[schedule_id] = {
        "name": (payload.name or "").strip() or f"Schedule {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "enabled": bool(payload.enabled),
        "weekdays": weekdays,
        "from_hour": int(payload.from_hour),
        "to_hour": int(payload.to_hour),
        "api_host": payload.api_host.strip(),
        "domain": payload.domain.strip(),
        "access_token": payload.access_token.strip(),
        "scenario_id": payload.scenario_id,
        "min_duration": int(payload.min_duration),
        "has_recording": bool(payload.has_recording),
        "records_limit": int(payload.records_limit),
        "whisper_model": normalize_whisper_model_name(payload.whisper_model),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "last_run_at": None,
        "last_run_slot": None,
        "last_result": None,
    }
    save_schedules()
    return {"status": "created", "id": schedule_id, "schedule": {"id": schedule_id, **persisted_schedules[schedule_id]}}


@app.patch("/api/schedules/{schedule_id}")
async def update_schedule(schedule_id: str, payload: UpdateScheduleRequest):
    schedule = persisted_schedules.get(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Расписание не найдено")

    if payload.enabled is not None:
        schedule["enabled"] = bool(payload.enabled)
    if payload.name is not None:
        schedule["name"] = payload.name.strip() or schedule.get("name")

    save_schedules()
    return {"status": "updated", "schedule": {"id": schedule_id, **schedule}}


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str):
    if schedule_id not in persisted_schedules:
        raise HTTPException(status_code=404, detail="Расписание не найдено")
    persisted_schedules.pop(schedule_id, None)
    save_schedules()
    return {"status": "deleted", "id": schedule_id}


@app.post("/api/schedules/{schedule_id}/run-now")
async def run_schedule_now(schedule_id: str):
    schedule = persisted_schedules.get(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Расписание не найдено")
    slot_key = f"manual-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    asyncio.create_task(run_schedule_for_current_hour(schedule_id, slot_key))
    return {"status": "started", "id": schedule_id}


@app.get("/api/queue")
async def get_queue_state():
    return {
        "items": sorted(
            [queue_item_to_public(item) for item in persisted_queue.values()],
            key=lambda row: row.get("created_at") or "",
            reverse=True,
        ),
        "counters": build_queue_counters(),
        "runner": {
            "running": bool(queue_runner_task and not queue_runner_task.done()),
            "stop_requested": queue_runner_stop_requested,
            "active_workers": len([task for task in queue_worker_tasks if not task.done()]),
        },
    }


@app.post("/api/queue/add")
async def queue_add(payload: QueueAddRequest):
    try:
        item = normalize_queue_item(
            payload.record,
            api_host=payload.api_host,
            domain=payload.domain,
            access_token=payload.access_token,
            whisper_model=payload.whisper_model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    existing = persisted_queue.get(item["call_id"])
    if existing:
        return {"status": "exists", "item": queue_item_to_public(existing), "counters": build_queue_counters()}

    cached = get_cached_transcript(item["call_id"], record_url=item.get("record_url"))
    if cached and cached.get("transcript"):
        item["status"] = "done"
        item["transcript_text"] = cached.get("transcript") or ""
        item["audio_url"] = cached.get("audio_url")

    persisted_queue[item["call_id"]] = item
    save_queue()
    await emit_queue_snapshot()
    logger.info(f"[QUEUE] Added call to queue: {item['call_id']} status={item['status']}")
    return {"status": "added", "item": queue_item_to_public(item), "counters": build_queue_counters()}


@app.post("/api/queue/add-bulk")
async def queue_add_bulk(payload: QueueAddBulkRequest):
    added = 0
    existed = 0
    done_from_cache = 0

    for record in payload.records:
        try:
            item = normalize_queue_item(
                record,
                api_host=payload.api_host,
                domain=payload.domain,
                access_token=payload.access_token,
                whisper_model=payload.whisper_model,
            )
        except ValueError:
            continue

        if item["call_id"] in persisted_queue:
            existed += 1
            continue

        cached = get_cached_transcript(item["call_id"], record_url=item.get("record_url"))
        if cached and cached.get("transcript"):
            item["status"] = "done"
            item["transcript_text"] = cached.get("transcript") or ""
            item["audio_url"] = cached.get("audio_url")
            done_from_cache += 1

        persisted_queue[item["call_id"]] = item
        added += 1

    save_queue()
    await emit_queue_snapshot()
    logger.info(f"[QUEUE] Bulk add completed: added={added}, existed={existed}, done_from_cache={done_from_cache}")
    return {
        "status": "ok",
        "added": added,
        "existed": existed,
        "done_from_cache": done_from_cache,
        "counters": build_queue_counters(),
    }


@app.post("/api/queue/start")
async def queue_start():
    global queue_runner_task, queue_runner_stop_requested
    async with queue_runner_start_lock:
        if queue_runner_task and not queue_runner_task.done():
            return {"status": "already_running"}

        has_queued = any(item.get("status") == "queued" for item in persisted_queue.values())
        if not has_queued:
            return {"status": "nothing_to_process", "counters": build_queue_counters()}

        queue_runner_stop_requested = False
        queue_runner_task = asyncio.create_task(queue_runner_loop())
        await emit_queue_snapshot()
        return {"status": "started", "counters": build_queue_counters()}


@app.post("/api/queue/stop")
async def queue_stop(payload: QueueStopRequest):
    global queue_runner_stop_requested
    queue_runner_stop_requested = True

    canceled = 0
    if payload.cancel_queued:
        for item in persisted_queue.values():
            if item.get("status") == "queued":
                item["status"] = "canceled"
                item["updated_at"] = now_utc_iso()
                canceled += 1
        save_queue()

    await emit_queue_snapshot()
    return {"status": "stop_requested", "canceled": canceled, "counters": build_queue_counters()}


@app.post("/api/queue/retry-failed")
async def queue_retry_failed():
    retried = 0
    for item in persisted_queue.values():
        if item.get("status") == "failed":
            item["status"] = "queued"
            item["error_message"] = None
            item["updated_at"] = now_utc_iso()
            retried += 1
    save_queue()
    await emit_queue_snapshot()
    return {"status": "ok", "retried": retried, "counters": build_queue_counters()}


@app.post("/api/queue/retry-skipped")
async def queue_retry_skipped():
    retried = 0
    for item in persisted_queue.values():
        if item.get("status") == "skipped":
            item["status"] = "queued"
            item["error_message"] = None
            item["updated_at"] = now_utc_iso()
            retried += 1
    save_queue()
    await emit_queue_snapshot()
    return {"status": "ok", "retried": retried, "counters": build_queue_counters()}


@app.post("/api/queue/clear")
async def queue_clear(payload: QueueClearRequest):
    requested_statuses = {status for status in payload.statuses if status in QUEUE_STATUSES}
    if not requested_statuses:
        requested_statuses = {"done", "failed", "skipped"}

    removed_ids = []
    for call_id, item in list(persisted_queue.items()):
        if item.get("status") == "processing":
            continue
        if item.get("status") in requested_statuses:
            removed_ids.append(call_id)
            persisted_queue.pop(call_id, None)

    save_queue()
    await emit_queue_snapshot()
    return {"status": "ok", "removed": len(removed_ids), "counters": build_queue_counters()}


@app.delete("/api/queue/{call_id}")
async def queue_delete_item(call_id: str):
    normalized_call_id = str(call_id)
    item = persisted_queue.get(normalized_call_id)
    if not item:
        raise HTTPException(status_code=404, detail="Элемент очереди не найден")
    if item.get("status") == "processing":
        raise HTTPException(status_code=400, detail="Нельзя удалить элемент в статусе processing")

    persisted_queue.pop(normalized_call_id, None)
    save_queue()
    await emit_queue_snapshot()
    return {"status": "deleted", "call_id": normalized_call_id, "counters": build_queue_counters()}


@app.post("/api/load-more")
async def load_more(request: LoadMoreRequest, background_tasks: BackgroundTasks):
    session_id = request.session_id

    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail=f"Сессия {session_id} не найдена")

    session = active_sessions[session_id]
    pagination = session.get("pagination")

    if not pagination or not pagination.get("can_load_more"):
        raise HTTPException(status_code=400, detail="Невозможно загрузить дополнительные записи")

    cursor = pagination.get("cursor")
    if not cursor:
        raise HTTPException(status_code=400, detail="Курсор не найден")

    req_params = pagination.get("request", {})

    background_tasks.add_task(run_load_more_processing, session_id, cursor, req_params)

    return {"status": "loading", "message": "Загрузка дополнительных записей начата..."}


async def run_load_more_processing(session_id: str, cursor: str, req_params: dict) -> None:
    if session_id not in active_sessions:
        logger.warning(f"[LOAD_MORE] Сессия {session_id} не найдена")
        return

    api_host = req_params.get("api_host", "")
    domain = req_params.get("domain", "")
    access_token = req_params.get("access_token", "")
    from_date = req_params.get("from_date", "")
    to_date = req_params.get("to_date", "")
    scenario_id = req_params.get("scenario_id")
    min_duration = req_params.get("min_duration", 0)
    has_recording = req_params.get("has_recording", True)
    records_limit = req_params.get("records_limit", 100)
    max_pages = get_dynamic_page_budget(records_limit)

    api_host, account_name = resolve_api_host_and_account(api_host, domain)

    if not api_host or not account_name or not access_token:
        await send_ws_message(session_id, {
            "type": "status",
            "message": "Ошибка: некорректные параметры сессии",
        })
        return

    from_value, to_value, _ = clamp_datetime_range_to_now(from_date, to_date)

    api_url = f"https://{api_host}/api/v4/history/searchCalls?domain={account_name}"

    current_cursor = cursor
    call_count = 0
    page_number = 0
    initial_total_loaded = active_sessions[session_id]["pagination"].get("total_loaded", 0)

    await send_ws_message(session_id, {
        "type": "status",
        "message": "Продолжаю загрузку дополнительных звонков...",
    })

    async with httpx.AsyncClient(timeout=120) as client:
        while page_number < max_pages:
            page_number += 1
            form_data = {
                "access_token": access_token,
                "from": from_value,
                "to": to_value,
                "with_scenarios": "true",
                "limit": SEARCH_PAGE_SIZE,
                "cursor": current_cursor,
            }
            if scenario_id:
                form_data["scenario_ids"] = f"[{scenario_id}]"

            try:
                payload = await fetch_voximplant_page(client, api_url, form_data)
            except Exception as exc:
                await send_ws_message(session_id, {
                    "type": "status",
                    "message": f"Ошибка при загрузке дополнительных записей: {str(exc)}",
                })
                return

            results = payload.get("result") or []
            if not isinstance(results, list):
                results = []

            for item in results:
                # Проверить лимит перед добавлением записи
                if initial_total_loaded + call_count >= records_limit:
                    logger.info(f"[LOAD_MORE] Достигнут лимит записей: {initial_total_loaded + call_count}")
                    meta = payload.get("_meta") or {}
                    next_cursor = meta.get("cursor")
                    if next_cursor:
                        active_sessions[session_id]["pagination"]["cursor"] = next_cursor
                        active_sessions[session_id]["pagination"]["can_load_more"] = True
                        await send_ws_message(session_id, {
                            "type": "status",
                            "message": f"Загружено ещё {call_count} звонков. Доступны дополнительные записи.",
                        })
                        await send_ws_message(session_id, {
                            "type": "pagination_status",
                            "can_load_more": True,
                            "loaded": initial_total_loaded + call_count,
                            "limit": records_limit,
                        })
                    else:
                        active_sessions[session_id]["pagination"]["can_load_more"] = False
                    return

                record_url = item.get("record_url")
                duration = int(item.get("duration") or 0)
                if has_recording and not record_url:
                    continue
                if min_duration and duration < min_duration:
                    continue

                call_id = str(item.get("id"))
                cached = get_cached_transcript(call_id, record_url=record_url)
                call = {
                    "id": call_id,
                    "datetime_start": item.get("datetime_start"),
                    "timezone": extract_call_timezone(item),
                    "phone_a": item.get("phone_a"),
                    "phone_b": item.get("phone_b"),
                    "duration": duration,
                    "record_url": record_url,
                    "scenario": item.get("scenario", {}),
                    "transcript": cached.get("transcript") if cached else None,
                    "local_audio_url": cached.get("audio_url") if cached else None,
                }
                active_sessions[session_id]["calls"].append(call)
                await send_ws_message(session_id, {
                    "type": "record",
                    "record": call,
                })
                call_count += 1

            meta = payload.get("_meta") or {}
            current_cursor = meta.get("cursor")

            # total_loaded = уже загруженные до нажатия + новые в этой порции
            total_loaded = initial_total_loaded + call_count
            active_sessions[session_id]["pagination"]["total_loaded"] = total_loaded
            active_sessions[session_id]["pagination"]["cursor"] = current_cursor

            if not current_cursor:
                logger.info(f"[LOAD_MORE] Больше нет записей (загружено в этой порции: {call_count})")
                active_sessions[session_id]["pagination"]["can_load_more"] = False
                break

            # Если есть cursor и мы не достигли лимита, продолжить загрузку
            if total_loaded < records_limit:
                await asyncio.sleep(SEARCH_RETRY_DELAY_SECONDS)

    total_loaded = active_sessions[session_id]["pagination"].get("total_loaded", 0)
    can_load_more = bool(current_cursor) and total_loaded < records_limit
    active_sessions[session_id]["pagination"]["can_load_more"] = can_load_more
    await send_ws_message(session_id, {
        "type": "status",
        "message": f"Загружено ещё {call_count} звонков. Всего: {total_loaded}",
    })
    await send_ws_message(session_id, {
        "type": "pagination_status",
        "can_load_more": can_load_more,
        "loaded": total_loaded,
        "limit": records_limit,
    })


def parse_datetime(value: str) -> str:
    if not value:
        return ""

    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


async def fetch_voximplant_page(client: httpx.AsyncClient, api_url: str, form_data: dict) -> dict:
    for attempt in range(MAX_SEARCH_RETRIES + 1):
        response = await client.post(
            api_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=form_data,
        )

        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            delay = float(retry_after) if retry_after else SEARCH_RETRY_DELAY_SECONDS * (attempt + 1)
            logger.warning(f"[VOXIMPLANT] 429 rate limit, ждём {delay} сек before retry #{attempt + 1}")
            if attempt >= MAX_SEARCH_RETRIES:
                response.raise_for_status()
            await asyncio.sleep(delay)
            continue

        response.raise_for_status()
        return response.json()

    raise RuntimeError("Не удалось получить страницу звонков из Voximplant")


def get_safe_filename(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix
    if suffix.lower() not in {".mp3", ".wav", ".ogg", ".m4a", ".flac"}:
        suffix = ".mp3"
    stable_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return f"{stable_hash}{suffix}"


def build_audio_url(audio_path: Path) -> str:
    return f"http://localhost:8000/audio/{audio_path.name}"


def get_audio_download_lock(destination: Path) -> asyncio.Lock:
    destination_key = str(destination.resolve())
    lock = audio_download_locks.get(destination_key)
    if lock is None:
                lock = asyncio.Lock()
                audio_download_locks[destination_key] = lock
    return lock


async def download_audio(record_url: str, destination: Path, access_token: Optional[str] = None, api_host: Optional[str] = None, domain: Optional[str] = None) -> None:
    if not record_url:
        raise RuntimeError("Пустой URL записи")

    lock = get_audio_download_lock(destination)
    async with lock:
        # Reuse previously downloaded audio to avoid playback delays during CPU-heavy transcription.
        if destination.exists() and destination.stat().st_size > 0:
            return

        parsed_url = urlparse(record_url)
        if not parsed_url.scheme or not parsed_url.netloc:
            raise RuntimeError(f"Некорректный URL записи: {record_url}")

        query_params = parse_qs(parsed_url.query)
        call_id = query_params.get("call_id", [None])[0]

        payload = {}
        if access_token:
            payload["access_token"] = access_token
        if call_id:
            payload["call_id"] = call_id
        if domain:
            payload["domain"] = domain
        if api_host:
            payload["api_host"] = api_host

        async with httpx.AsyncClient(timeout=180) as client:
            last_error = None
            methods = []

            if payload:
                methods.append(("post", {"data": payload, "headers": {"Content-Type": "application/x-www-form-urlencoded"}}))
            methods.append(("get", {"params": payload}))

            for method, kwargs in methods:
                try:
                    request_url = parsed_url.geturl() or record_url
                    logger.info(f"[AUDIO] URL запроса: {request_url}")
                    if method == "post":
                        response = await client.post(request_url, **kwargs)
                    else:
                        response = await client.get(request_url, **kwargs)

                    logger.info(f"[AUDIO] Попытка загрузки аудио через {method.upper()}, status={response.status_code}")
                    if response.status_code < 400:
                        if not response.content:
                            logger.warning("[AUDIO] Аудио ответ пришёл пустым")
                        async with aiofiles.open(destination, "wb") as out_file:
                            await out_file.write(response.content)
                        return

                    last_error = f"{response.status_code} {response.reason_phrase}"
                    logger.warning(f"[AUDIO] Загрузка не удалась: {last_error}")
                except Exception as exc:
                    last_error = str(exc)
                    logger.warning(f"[AUDIO] Ошибка загрузки через {method.upper()}: {exc}")

            raise RuntimeError(f"Не удалось скачать аудио: {last_error}")


def normalize_audio(audio_path: Path) -> Path:
    """Загружает аудио, нормализует и сохраняет в стандартный формат (16kHz, mono, float32)."""
    logger.info(f"[AUDIO] Загрузка аудиофайла: {audio_path}")

    try:
        file_size_bytes = audio_path.stat().st_size
        file_size_kb = round(file_size_bytes / 1024, 2)
        logger.info(f"[AUDIO] Имя файла: {audio_path.name}")
        logger.info(f"[AUDIO] Размер файла: {file_size_bytes} bytes ({file_size_kb} KB)")

        y, sr = librosa.load(str(audio_path), sr=16000, mono=True)
        if y is None or y.size == 0:
            raise RuntimeError("Аудиофайл не содержит семплов после декодирования")

        min_samples = int(16000 * MIN_WHISPER_AUDIO_SECONDS)
        if y.size < min_samples:
            pad_count = min_samples - y.size
            y = np.pad(y, (0, pad_count), mode="constant")
            logger.info(f"[AUDIO] Аудио слишком короткое, добавлено {pad_count} пустых семплов")

        duration_seconds = round(len(y) / sr, 3)

        logger.info(f"[AUDIO] Частота дискретизации: {sr} Hz")
        logger.info(f"[AUDIO] Длительность: {duration_seconds} сек")
        logger.info(f"[AUDIO] Форма аудио: {y.shape}, dtype: {y.dtype}")
        logger.info(f"[AUDIO] Тишина: min={y.min():.4f}, max={y.max():.4f}, mean={y.mean():.4f}")

        if np.abs(y).max() < 0.01:
            logger.warning(f"[AUDIO] ВНИМАНИЕ: Аудиосигнал очень слабый (max amplitude < 0.01)")

        normalized_path = audio_path.with_suffix('.wav')
        y_int16 = np.int16(y * 32767)
        wavfile.write(str(normalized_path), 16000, y_int16)
        logger.info(f"[AUDIO] Нормализованное аудио сохранено: {normalized_path}")

        return normalized_path
    except Exception as exc:
        logger.error(f"[AUDIO] Ошибка при нормализации: {str(exc)}", exc_info=True)
        raise


def ensure_min_audio_duration(audio_path: Path) -> Path:
    """Гарантирует минимальную длительность WAV для стабильной работы Whisper."""
    y, sr = librosa.load(str(audio_path), sr=16000, mono=True)
    if y is None or y.size == 0:
        raise RuntimeError("Аудио пустое после нормализации")

    min_samples = int(16000 * MIN_WHISPER_AUDIO_SECONDS)
    if y.size >= min_samples:
        return audio_path

    y = np.pad(y, (0, min_samples - y.size), mode="constant")
    y_int16 = np.int16(y * 32767)
    wavfile.write(str(audio_path), 16000, y_int16)
    logger.info("[AUDIO] Применен fallback-padding для короткого аудио")
    return audio_path


async def run_processing(request: ProcessRequest) -> None:
    await send_ws_message(request.session_id, {
        "type": "status",
        "message": "Подключение к Voximplant Kit и получение списка звонков...",
    })

    api_host, account_name = resolve_api_host_and_account(request.api_host, request.domain)

    if not api_host:
        await send_ws_message(request.session_id, {
            "type": "status",
            "message": "Не указан хост API (host).",
        })
        return

    if not account_name:
        await send_ws_message(request.session_id, {
            "type": "status",
            "message": "Не указан домен аккаунта (domain).",
        })
        return

    api_url = f"https://{api_host}/api/v4/history/searchCalls?domain={account_name}"
    access_token = request.access_token
    if not access_token:
        await send_ws_message(request.session_id, {
            "type": "status",
            "message": "Не указан access_token.",
        })
        return

    from_value, to_value, to_was_clamped = clamp_datetime_range_to_now(request.from_date, request.to_date)
    if to_was_clamped:
        await send_ws_message(request.session_id, {
            "type": "status",
            "message": "Время 'до' превышало текущий момент и было автоматически ограничено текущим временем.",
        })
    scenario_ids = None
    if request.scenario_id:
        scenario_ids = f"[{request.scenario_id}]"

    cursor = None
    call_count = 0
    raw_count = 0
    filtered_out_count = 0
    page_number = 0
    max_pages = get_dynamic_page_budget(request.records_limit)

    # Сохранить параметры запроса для LoadMore
    active_sessions[request.session_id]["pagination"] = {
        "request": {
            "api_host": request.api_host,
            "domain": request.domain,
            "access_token": request.access_token,
            "from_date": from_value,
            "to_date": to_value,
            "scenario_id": request.scenario_id,
            "min_duration": request.min_duration,
            "has_recording": request.has_recording,
            "records_limit": request.records_limit,
        },
        "cursor": None,
        "total_loaded": 0,
        "can_load_more": False,
    }

    async with httpx.AsyncClient(timeout=120) as client:
        while page_number < max_pages:
            page_number += 1
            form_data = {
                "access_token": access_token,
                "from": from_value,
                "to": to_value,
                "with_scenarios": "true",
                "limit": SEARCH_PAGE_SIZE,
            }
            if request.scenario_id:
                form_data["scenario_ids"] = f"[{request.scenario_id}]"
            if cursor:
                form_data["cursor"] = cursor

            try:
                payload = await fetch_voximplant_page(client, api_url, form_data)
            except httpx.HTTPStatusError as exc:
                details = None
                try:
                    details = exc.response.text
                except Exception:
                    details = str(exc)
                await send_ws_message(request.session_id, {
                    "type": "status",
                    "message": f"Ошибка при запросе звонков: {exc.response.status_code} {exc.response.reason_phrase} — {details}",
                })
                return
            except Exception as exc:
                await send_ws_message(request.session_id, {
                    "type": "status",
                    "message": f"Ошибка при запросе звонков: {str(exc)}",
                })
                return

            results = payload.get("result") or []
            if not isinstance(results, list):
                results = []
            raw_count += len(results)

            for item in results:
                # Проверить лимит перед добавлением записи
                if call_count >= request.records_limit:
                    logger.info(f"[PAGINATION] Достигнут лимит записей: {call_count}")
                    meta = payload.get("_meta") or {}
                    next_cursor = meta.get("cursor")
                    if next_cursor:
                        active_sessions[request.session_id]["pagination"]["cursor"] = next_cursor
                        active_sessions[request.session_id]["pagination"]["can_load_more"] = True
                        await send_ws_message(request.session_id, {
                            "type": "status",
                            "message": f"Загружено {call_count} из {request.records_limit} звонков. Доступны дополнительные записи.",
                        })
                        await send_ws_message(request.session_id, {
                            "type": "pagination_status",
                            "can_load_more": True,
                            "loaded": call_count,
                            "limit": request.records_limit,
                        })
                    return

                record_url = item.get("record_url")
                duration = int(item.get("duration") or 0)
                if request.has_recording and not record_url:
                    filtered_out_count += 1
                    continue
                if request.min_duration and duration < request.min_duration:
                    filtered_out_count += 1
                    continue

                call_id = str(item.get("id"))
                cached = get_cached_transcript(call_id, record_url=record_url)
                call = {
                    "id": call_id,
                    "datetime_start": item.get("datetime_start"),
                    "timezone": extract_call_timezone(item),
                    "phone_a": item.get("phone_a"),
                    "phone_b": item.get("phone_b"),
                    "duration": duration,
                    "record_url": record_url,
                    "scenario": item.get("scenario", {}),
                    "transcript": cached.get("transcript") if cached else None,
                    "local_audio_url": cached.get("audio_url") if cached else None,
                }
                active_sessions[request.session_id]["calls"].append(call)
                await send_ws_message(request.session_id, {
                    "type": "record",
                    "record": call,
                })
                call_count += 1

            meta = payload.get("_meta") or {}
            cursor = meta.get("cursor")
            
            # Сохранить текущий cursor и общее количество загруженных записей
            active_sessions[request.session_id]["pagination"]["cursor"] = cursor
            active_sessions[request.session_id]["pagination"]["total_loaded"] = call_count

            if call_count >= request.records_limit:
                if cursor:
                    active_sessions[request.session_id]["pagination"]["can_load_more"] = True
                    await send_ws_message(request.session_id, {
                        "type": "status",
                        "message": f"Достигнут предел страницы ({request.records_limit} звонков). Можно загрузить ещё записи.",
                    })
                    await send_ws_message(request.session_id, {
                        "type": "pagination_status",
                        "can_load_more": True,
                        "loaded": call_count,
                        "limit": request.records_limit,
                    })
                return

            if not cursor:
                logger.info(f"[PAGINATION] Больше нет записей (всего загружено: {call_count})")
                await send_ws_message(request.session_id, {
                    "type": "status",
                    "message": (
                        f"По выбранному интервалу и фильтрам больше записей нет. "
                        f"Получено от API: {raw_count}, загружено в таблицу: {call_count}, отфильтровано: {filtered_out_count}."
                    ),
                })
                break

            # Если есть cursor и мы не достигли лимита, продолжить загрузку
            await asyncio.sleep(SEARCH_RETRY_DELAY_SECONDS)

    if cursor and call_count < request.records_limit and page_number >= max_pages:
        await send_ws_message(request.session_id, {
            "type": "status",
            "message": (
                f"Остановлено по внутреннему лимиту страниц ({max_pages}). "
                f"Получено от API: {raw_count}, загружено: {call_count}. "
                "Можно продолжить кнопкой 'Загрузить ещё записи'."
            ),
        })

    await send_ws_message(request.session_id, {
        "type": "status",
        "message": (
            f"Найдено звонков: {call_count}. "
            f"Получено от API: {raw_count}, отфильтровано: {filtered_out_count}. Завершено поиск."
        ),
    })
    # Если дошли до технического лимита страниц, но курсор ещё есть,
    # разрешаем пользователю продолжить загрузку кнопкой "Загрузить ещё".
    can_load_more = bool(cursor) and call_count < request.records_limit
    active_sessions[request.session_id]["pagination"]["can_load_more"] = can_load_more
    await send_ws_message(request.session_id, {
        "type": "pagination_status",
        "can_load_more": can_load_more,
        "loaded": call_count,
        "limit": request.records_limit,
    })
    await send_ws_message(request.session_id, {
        "type": "status",
        "message": "Готово! Обработка завершена.",
    })


app.mount("/audio", StaticFiles(directory="audio-cache"), name="audio")
