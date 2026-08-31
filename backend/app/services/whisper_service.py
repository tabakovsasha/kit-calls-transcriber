"""Whisper model management and audio transcription.

Ported from the legacy single-file app with the same defensive behaviour:
- models are loaded once and shared, with concurrent loads deduplicated;
- inference is serialized per model instance by default (CPU safety);
- degenerate audio is detected and skipped rather than crashing the worker;
- every blocking call runs in a thread with a hard timeout.

Extended with download/delete operations and status tracking for the UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from app.core.config import get_settings
from app.core.runtime import runtime
from app.schemas.transcription import (
    SUPPORTED_WHISPER_MODELS,
    normalize_whisper_model_name,
)

logger = logging.getLogger(__name__)

MODELS_DIR = Path("whisper-models")
MIN_WHISPER_AUDIO_SECONDS = 1.0
MIN_AUDIO_FILE_BYTES = 100
MIN_AUDIO_SECONDS_FOR_TRANSCRIBE = 0.1
SAMPLE_RATE = 16000

# Approximate model sizes in MB (for UI display and concurrency planning)
MODEL_SIZES_MB = {
    "tiny": 75, "tiny.en": 75,
    "base": 145, "base.en": 145,
    "small": 488, "small.en": 488,
    "medium": 1540, "medium.en": 1540,
    "large": 3100, "large-v1": 3100, "large-v2": 3100, "large-v3": 3100,
    "turbo": 1600, "large-v3-turbo": 1600,
}

_loaded_models: dict[str, Any] = {}
_loading_tasks: dict[str, asyncio.Task] = {}
_model_states: dict[str, dict[str, Any]] = {}
_inference_locks: dict[str, asyncio.Lock] = {}


def ensure_models_dir() -> Path:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return MODELS_DIR


def is_multilingual_model(model_name: str) -> bool:
    return not model_name.endswith(".en")


def get_model_status(model_name: str) -> dict[str, Any]:
    """Report whether a model is cached on disk or currently downloading."""
    ensure_models_dir()
    aliases = {"turbo": ["large-v3-turbo.pt", "turbo.pt"]}
    candidates = [MODELS_DIR / model_name, MODELS_DIR / f"{model_name}.pt"]
    candidates.extend(MODELS_DIR / alias for alias in aliases.get(model_name, []))

    existing = next((path for path in candidates if path.exists()), None)
    state = _model_states.get(model_name, {"status": "missing", "progress": 0, "error": None})

    if existing and state.get("status") in {"missing", None}:
        return {"status": "ready", "progress": 100, "error": None, "cached": True}
    if existing:
        return {**state, "cached": True}
    return {**state, "cached": False}


def list_model_statuses() -> list[dict[str, Any]]:
    statuses = []
    for name in SUPPORTED_WHISPER_MODELS:
        status = get_model_status(name)
        statuses.append({
            "name": name,
            "size_mb": MODEL_SIZES_MB.get(name, 1500),
            **status,
        })
    return statuses


async def delete_model(model_name: str) -> dict[str, Any]:
    """Remove a downloaded model from disk and unload it from memory.
    
    Returns status dict. Never raises; errors are returned in the dict.
    """
    normalized = normalize_whisper_model_name(model_name)
    
    # Don't delete a model that's currently in use
    if normalized in _loaded_models:
        return {
            "success": False,
            "error": "Модель используется. Дождитесь завершения активных транскрибаций.",
        }
    
    # Don't delete a model that's currently downloading
    if normalized in _loading_tasks:
        return {
            "success": False,
            "error": "Модель скачивается. Дождитесь завершения.",
        }
    
    ensure_models_dir()
    aliases = {"turbo": ["large-v3-turbo.pt", "turbo.pt"]}
    candidates = [MODELS_DIR / normalized, MODELS_DIR / f"{normalized}.pt"]
    candidates.extend(MODELS_DIR / alias for alias in aliases.get(normalized, []))
    
    deleted = []
    for path in candidates:
        if path.exists():
            try:
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
                deleted.append(str(path.name))
            except Exception as exc:
                logger.warning("[WHISPER] Не удалось удалить %s: %s", path, exc)
                return {"success": False, "error": f"Ошибка удаления файла: {exc}"}
    
    # Clear any cached state
    _model_states.pop(normalized, None)
    
    if not deleted:
        return {"success": False, "error": "Модель не найдена на диске"}
    
    return {"success": True, "deleted": deleted}


def _inference_lock(model_name: str) -> asyncio.Lock:
    lock = _inference_locks.get(model_name)
    if lock is None:
        lock = asyncio.Lock()
        _inference_locks[model_name] = lock
    return lock


@contextlib.asynccontextmanager
async def maybe_inference_lock(model_name: str):
    """Serialize inference per model unless explicitly configured otherwise."""
    if get_settings().whisper_inference_isolation == "serialized":
        async with _inference_lock(model_name):
            yield
    else:
        yield


async def get_or_load_model(model_name: str) -> Any:
    """Return a loaded Whisper model, deduplicating concurrent load requests."""
    normalized = normalize_whisper_model_name(model_name)
    if normalized in _loaded_models:
        return _loaded_models[normalized]

    existing = _loading_tasks.get(normalized)
    if existing is not None:
        return await existing

    _model_states[normalized] = {"status": "loading", "progress": 0, "error": None}

    async def _load() -> Any:
        import whisper

        try:
            model = await asyncio.to_thread(
                whisper.load_model,
                normalized,
                device="cpu",
                download_root=str(ensure_models_dir()),
            )
            _loaded_models[normalized] = model
            _model_states[normalized] = {"status": "ready", "progress": 100, "error": None}
            logger.info("[WHISPER] Модель готова: %s", normalized)
            return model
        except Exception as exc:
            _model_states[normalized] = {
                "status": "error",
                "progress": 0,
                "error": str(exc),
            }
            logger.error("[WHISPER] Ошибка загрузки модели %s: %s", normalized, exc)
            raise
        finally:
            _loading_tasks.pop(normalized, None)

    task = asyncio.create_task(_load())
    _loading_tasks[normalized] = task
    return await task


async def download_model_in_background(model_name: str) -> str:
    """Kick off a model download without blocking the request."""
    normalized = normalize_whisper_model_name(model_name)
    if normalized in _loaded_models:
        return normalized

    async def _job() -> None:
        try:
            await get_or_load_model(normalized)
        except Exception:
            # State already recorded by get_or_load_model.
            logger.warning("[WHISPER] Фоновая загрузка не удалась: %s", normalized)

    asyncio.create_task(_job())
    return normalized


# --- Audio preparation ------------------------------------------------------


def normalize_audio(audio_path: Path) -> Path:
    """Decode to 16 kHz mono WAV, padding audio that is too short for Whisper."""
    import librosa
    import numpy as np
    from scipy.io import wavfile

    samples, _ = librosa.load(str(audio_path), sr=SAMPLE_RATE, mono=True)
    if samples is None or samples.size == 0:
        raise RuntimeError("Аудиофайл не содержит семплов после декодирования")

    min_samples = int(SAMPLE_RATE * MIN_WHISPER_AUDIO_SECONDS)
    if samples.size < min_samples:
        samples = np.pad(samples, (0, min_samples - samples.size), mode="constant")

    normalized_path = audio_path.with_suffix(".wav")
    wavfile.write(str(normalized_path), SAMPLE_RATE, np.int16(samples * 32767))
    return normalized_path


def ensure_min_duration(audio_path: Path) -> Path:
    """Pad a WAV in place so Whisper's attention layers get a usable window."""
    import librosa
    import numpy as np
    from scipy.io import wavfile

    samples, _ = librosa.load(str(audio_path), sr=SAMPLE_RATE, mono=True)
    if samples is None or samples.size == 0:
        raise RuntimeError("Аудио пустое после нормализации")

    min_samples = int(SAMPLE_RATE * MIN_WHISPER_AUDIO_SECONDS)
    if samples.size >= min_samples:
        return audio_path

    samples = np.pad(samples, (0, min_samples - samples.size), mode="constant")
    wavfile.write(str(audio_path), SAMPLE_RATE, np.int16(samples * 32767))
    return audio_path


def _skipped_result(reason: str) -> dict[str, Any]:
    return {
        "text": "",
        "language": "unknown",
        "segments": [],
        "skipped": True,
        "error": reason,
    }


def _detect_invalid_audio(path: Path) -> Optional[str]:
    """Return why the file cannot be transcribed, or None when it is usable."""
    import whisper

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

    duration_seconds = float(audio.shape[0]) / float(SAMPLE_RATE)
    if duration_seconds < MIN_AUDIO_SECONDS_FOR_TRANSCRIBE:
        return f"Аудио слишком короткое ({duration_seconds:.3f}s)"

    return None


async def transcribe_audio(
    model: Any, audio_path: Path, whisper_model: str
) -> tuple[dict[str, Any], str]:
    """Transcribe one file. Returns (raw_result, transcript_text).

    Degenerate audio yields a ``skipped`` result instead of an exception so a
    single bad recording cannot stall the queue.
    """
    settings = get_settings()

    transcribe_kwargs: dict[str, Any] = {
        "task": "transcribe",
        "fp16": False,
        "word_timestamps": False,
        "verbose": False,
        # None lets Whisper auto-detect; .en models must be pinned to English.
        "language": None if is_multilingual_model(whisper_model) else "en",
    }

    def _run_sync(path: Path, kwargs: dict[str, Any]) -> dict[str, Any]:
        # Whisper writes progress to stdout/stderr; keep our logs clean.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                return model.transcribe(str(path), **kwargs)
            except RuntimeError as exc:
                if "tensor of 0 elements" in str(exc):
                    return _skipped_result("Пустой/поврежденный аудиофайл (zero-tensor)")
                raise

    async def _run(path: Path, kwargs: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_run_sync, path, kwargs), timeout=timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"Таймаут транскрибации ({timeout_seconds} сек) для файла {path.name}"
            ) from exc

    invalid_reason = await asyncio.to_thread(_detect_invalid_audio, audio_path)
    if invalid_reason:
        logger.warning("[TRANSCRIBE] Пропуск аудио %s: %s", audio_path.name, invalid_reason)
        return _skipped_result(invalid_reason), ""

    primary_timeout = settings.whisper_transcribe_timeout_seconds
    try:
        result = await _run(audio_path, transcribe_kwargs, primary_timeout)
    except RuntimeError as exc:
        if "cannot reshape tensor of 0 elements" not in str(exc):
            raise
        # Very short segments produce zero-length tensors; retry padded.
        try:
            padded_path = await asyncio.to_thread(ensure_min_duration, audio_path)
            result = await _run(padded_path, transcribe_kwargs, primary_timeout)
        except RuntimeError as padded_exc:
            if "tensor of 0 elements" not in str(padded_exc):
                raise
            result = _skipped_result("Пустой/поврежденный аудиофайл после fallback-padding")

    if result.get("skipped"):
        return result, ""

    transcript = str(result.get("text") or "").strip()
    if transcript:
        return result, transcript

    # Empty text is often a decoding artifact: retry with conservative params.
    fallback_kwargs = {
        **transcribe_kwargs,
        "temperature": 0.0,
        "condition_on_previous_text": False,
    }
    fallback_kwargs.pop("word_timestamps", None)
    try:
        result = await _run(
            audio_path, fallback_kwargs, settings.whisper_fallback_timeout_seconds
        )
        transcript = str(result.get("text") or "").strip()
    except RuntimeError as exc:
        if "tensor of 0 elements" in str(exc):
            return _skipped_result("Fallback получил zero-tensor"), ""
        logger.warning("[TRANSCRIBE] Повторная транскрибация не удалась: %s", exc)

    return result, transcript

