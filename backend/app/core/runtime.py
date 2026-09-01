"""CPU/thread planning and concurrency limiting for Whisper inference.

Ported from the legacy single-file app. Two rules kept intact:
- torch thread counts can only be set once, at startup (changing them later
  hangs the process), so a profile change only resizes concurrency;
- concurrency * torch_threads must not oversubscribe the usable cores.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
from typing import Any, Dict, Optional

from app.core.config import get_settings
from app.core.errors import ValidationError

logger = logging.getLogger(__name__)

try:  # psutil is optional; physical core count degrades to logical.
    import psutil  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    psutil = None

LOGICAL_CPU_COUNT = max(1, os.cpu_count() or 1)
PHYSICAL_CPU_COUNT = max(
    1, (psutil.cpu_count(logical=False) if psutil else None) or LOGICAL_CPU_COUNT
)
MAX_TRANSCRIBE_CONCURRENCY_HARD_LIMIT = 12
PROFILE_MAX = "max"
PROFILE_MODERATE = "moderate"
SUPPORTED_PROFILES = (PROFILE_MAX, PROFILE_MODERATE)

# Peak resident memory per concurrent transcription, in MB. These are
# conservative estimates (weights in fp32 + activations + decoding buffers for a
# typical call recording), not measurements: the point is to stop the planner
# from promising parallelism the host cannot pay for. Keys mirror
# app.schemas.transcription.SUPPORTED_WHISPER_MODELS plus the aliases whisper
# itself accepts; duplicated here instead of imported to keep app.core free of
# any dependency on the schema layer.
MODEL_PEAK_RAM_MB: Dict[str, int] = {
    "tiny": 400,
    "tiny.en": 400,
    "base": 700,
    "base.en": 700,
    "small": 1500,
    "small.en": 1500,
    "medium": 3500,
    "medium.en": 3500,
    "large": 6500,
    "large-v1": 6500,
    "large-v2": 6500,
    "large-v3": 6500,
    "turbo": 4500,
    "large-v3-turbo": 4500,
}
DEFAULT_PLAN_MODEL = "base"

# Left free for the OS, Postgres and the API process itself. The moderate
# profile keeps a larger cushion than max.
RAM_RESERVE_MB = {PROFILE_MAX: 1024, PROFILE_MODERATE: 2048}
# Used when neither psutil nor /proc/meminfo can be read: assume a small host so
# the planner errs towards serial processing rather than an OOM kill.
FALLBACK_TOTAL_RAM_MB = 4096


def _available_ram_mb() -> tuple[int, str]:
    """Best-effort free memory in MB, with the source of the number."""
    if psutil is not None:
        try:
            return max(1, int(psutil.virtual_memory().available // (1024 * 1024))), "psutil"
        except Exception as exc:  # pragma: no cover - platform dependent
            logger.warning("[RUNTIME] psutil не смог прочитать память: %s", exc)

    # Linux fallback: MemAvailable is what the kernel thinks is really usable.
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return max(1, int(line.split()[1]) // 1024), "proc_meminfo"
    except Exception as exc:  # pragma: no cover - non-Linux hosts
        logger.warning("[RUNTIME] /proc/meminfo недоступен: %s", exc)

    return FALLBACK_TOTAL_RAM_MB, "fallback"


def _transcription_ram_budget_mb(profile: str) -> tuple[int, str]:
    """Memory the planner is allowed to spend on concurrent transcriptions."""
    available_mb, source = _available_ram_mb()
    reserve_mb = RAM_RESERVE_MB.get(profile, RAM_RESERVE_MB[PROFILE_MODERATE])
    # Never return 0: a single job is always allowed to run, even on a tight
    # host, because refusing all work is worse than swapping.
    return max(1, available_mb - reserve_mb), source


class DynamicConcurrencyLimiter:
    """Semaphore whose capacity can change while tasks are in flight."""

    def __init__(self, max_concurrency: int) -> None:
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
        async with self._condition:
            self._max_concurrency = max(1, int(max_concurrency))
            self._condition.notify_all()

    @contextlib.asynccontextmanager
    async def slot(self):
        async with self._condition:
            await self._condition.wait_for(lambda: self._in_use < self._max_concurrency)
            self._in_use += 1
        try:
            yield
        finally:
            async with self._condition:
                self._in_use = max(0, self._in_use - 1)
                self._condition.notify_all()


def calculate_performance_plan(
    profile: str, whisper_model: str = DEFAULT_PLAN_MODEL
) -> Dict[str, Any]:
    """Derive concurrency and thread counts from CPU topology and model weight.

    Model-aware on purpose: the same host can run several ``base`` jobs but only
    one ``large-v3``, so peak RAM per job caps concurrency independently of the
    core count. The RAM figures are conservative estimates, not benchmarks.
    """
    normalized = profile if profile in SUPPORTED_PROFILES else PROFILE_MODERATE
    model = whisper_model if whisper_model in MODEL_PEAK_RAM_MB else DEFAULT_PLAN_MODEL
    total_cores = max(1, PHYSICAL_CPU_COUNT)

    # Moderate keeps a real reserve for the OS, Postgres and the API itself:
    # at least 2 cores where the host can afford it, 25% on larger machines.
    if normalized == PROFILE_MAX:
        usable_cores = total_cores
    elif total_cores <= 2:
        usable_cores = 1
    elif total_cores <= 8:
        usable_cores = max(1, total_cores - 2)
    else:
        usable_cores = max(1, int(math.floor(total_cores * 0.75)))

    if usable_cores <= 2:
        concurrency = 1
    elif usable_cores <= 4:
        concurrency = 2
    elif usable_cores <= 8:
        concurrency = min(4, usable_cores)
    else:
        concurrency = min(8, usable_cores)

    cpu_concurrency = max(1, min(MAX_TRANSCRIBE_CONCURRENCY_HARD_LIMIT, concurrency))

    # RAM ceiling: peak inference memory per job against what is actually free,
    # minus a reserve so a spike cannot OOM the host.
    ram_per_job_mb = MODEL_PEAK_RAM_MB[model]
    budget_mb, ram_source = _transcription_ram_budget_mb(normalized)
    ram_concurrency = max(1, int(budget_mb // ram_per_job_mb))

    concurrency = max(1, min(cpu_concurrency, ram_concurrency))
    limited_by = "cpu" if concurrency == cpu_concurrency else "ram"

    # Serialized inference is the current architecture: one shared model
    # instance transcribes one file at a time, so advertising per-model
    # parallelism here would be a lie.
    isolation = get_settings().whisper_inference_isolation
    if isolation == "serialized" and concurrency > 1:
        limited_by = "inference_isolation"

    torch_threads = max(1, usable_cores // concurrency)

    # On CPU, fewer parallel tasks with more threads each wins.
    while concurrency > 1 and torch_threads < 2 and usable_cores >= 4:
        concurrency -= 1
        torch_threads = max(1, usable_cores // concurrency)

    return {
        "profile": normalized,
        "whisper_model": model,
        # Inference runs on CPU: whisper_service loads every model with
        # device="cpu" and fp16=False. Reported here so the UI states the real
        # device instead of inferring one from torch's CUDA availability.
        "device": "cpu",
        "physical_cores": total_cores,
        "logical_cores": LOGICAL_CPU_COUNT,
        "usable_cores": usable_cores,
        "reserved_cores": max(0, total_cores - usable_cores),
        "max_concurrency": concurrency,
        "queue_workers": max(1, min(concurrency, usable_cores)),
        "torch_num_threads": torch_threads,
        "torch_num_interop_threads": 1,
        "cpu_concurrency": cpu_concurrency,
        "ram_concurrency": ram_concurrency,
        "ram_per_job_mb": ram_per_job_mb,
        "ram_budget_mb": budget_mb,
        "ram_source": ram_source,
        "limited_by": limited_by,
        "inference_isolation": isolation,
    }


def _apply_settings_overrides(plan: Dict[str, Any]) -> Dict[str, Any]:
    settings = get_settings()
    updated = {**plan}

    if settings.max_transcribe_concurrency is not None:
        updated["max_concurrency"] = max(
            1,
            min(MAX_TRANSCRIBE_CONCURRENCY_HARD_LIMIT, settings.max_transcribe_concurrency),
        )
    if settings.torch_num_threads is not None:
        updated["torch_num_threads"] = max(1, min(LOGICAL_CPU_COUNT, settings.torch_num_threads))

    # Prevent thread oversubscription across concurrent transcriptions.
    threads_cap = max(1, updated["usable_cores"] // max(1, updated["max_concurrency"]))
    if updated["torch_num_threads"] > threads_cap:
        updated["torch_num_threads"] = threads_cap

    updated["torch_num_interop_threads"] = 1
    updated["queue_workers"] = max(1, min(updated["queue_workers"], updated["max_concurrency"]))
    return updated


class RuntimePerformance:
    """Single source of truth for transcription runtime settings."""

    def __init__(self) -> None:
        settings = get_settings()
        self._plan = _apply_settings_overrides(
            calculate_performance_plan(settings.transcribe_performance_profile)
        )
        self._lock = asyncio.Lock()
        self._torch_configured = False
        self.limiter = DynamicConcurrencyLimiter(self._plan["max_concurrency"])
        self.active_transcriptions = 0
        self.active_inference_tasks = 0

    @property
    def plan(self) -> Dict[str, Any]:
        return dict(self._plan)

    @property
    def torch_configured(self) -> bool:
        return self._torch_configured

    def get(self, key: str) -> Any:
        return self._plan[key]

    def configure_torch_threads(self) -> None:
        """Set torch thread counts exactly once, at startup."""
        if self._torch_configured:
            return
        try:
            import torch

            torch.set_num_interop_threads(self._plan["torch_num_interop_threads"])
            torch.set_num_threads(self._plan["torch_num_threads"])
        except ImportError:
            logger.warning("[RUNTIME] torch не установлен, пропускаю настройку потоков")
            return
        except (RuntimeError, ValueError) as exc:
            # Interop threads can only be set before the first parallel op.
            logger.warning("[RUNTIME] Не удалось настроить потоки torch: %s", exc)
            self._torch_configured = True
            return

        self._torch_configured = True
        logger.info(
            "[RUNTIME] torch threads=%s interop=%s concurrency=%s profile=%s",
            self._plan["torch_num_threads"],
            self._plan["torch_num_interop_threads"],
            self._plan["max_concurrency"],
            self._plan["profile"],
        )

    async def apply_profile(
        self, profile: str, *, whisper_model: Optional[str] = None
    ) -> Dict[str, Any]:
        """Switch profile and/or default model. Torch threads stay fixed.

        Only the fields that are safe to change at runtime are copied over:
        ``torch_num_threads`` is deliberately excluded because torch hangs if the
        thread count is changed after the first parallel op.
        """
        normalized = (profile or "").strip().lower()
        if normalized not in SUPPORTED_PROFILES:
            raise ValidationError("Недопустимый профиль производительности")

        model = whisper_model or self._plan.get("whisper_model", DEFAULT_PLAN_MODEL)
        new_plan = _apply_settings_overrides(calculate_performance_plan(normalized, model))
        async with self._lock:
            for key in (
                "profile",
                "whisper_model",
                "usable_cores",
                "reserved_cores",
                "max_concurrency",
                "queue_workers",
                "cpu_concurrency",
                "ram_concurrency",
                "ram_per_job_mb",
                "ram_budget_mb",
                "ram_source",
                "limited_by",
                "inference_isolation",
            ):
                self._plan[key] = new_plan[key]
            await self.limiter.resize(new_plan["max_concurrency"])
        logger.info(
            "[RUNTIME] Профиль=%s модель=%s concurrency=%s (cpu=%s ram=%s, ограничение=%s)",
            new_plan["profile"],
            new_plan["whisper_model"],
            new_plan["max_concurrency"],
            new_plan["cpu_concurrency"],
            new_plan["ram_concurrency"],
            new_plan["limited_by"],
        )
        return self.plan

    async def apply_model(self, whisper_model: str) -> Dict[str, Any]:
        """Recompute the plan for a new default model, keeping the profile."""
        return await self.apply_profile(
            self._plan.get("profile", PROFILE_MODERATE), whisper_model=whisper_model
        )


runtime = RuntimePerformance()

