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
from typing import Any, Dict

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


def calculate_performance_plan(profile: str) -> Dict[str, Any]:
    """Derive concurrency and thread counts from the CPU topology."""
    normalized = profile if profile in SUPPORTED_PROFILES else PROFILE_MODERATE
    total_cores = max(1, PHYSICAL_CPU_COUNT)
    usable_cores = (
        total_cores if normalized == PROFILE_MAX else max(1, int(math.floor(total_cores * 0.75)))
    )

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

    # On CPU, fewer parallel tasks with more threads each wins.
    while concurrency > 1 and torch_threads < 2 and usable_cores >= 4:
        concurrency -= 1
        torch_threads = max(1, usable_cores // concurrency)

    return {
        "profile": normalized,
        "physical_cores": total_cores,
        "logical_cores": LOGICAL_CPU_COUNT,
        "usable_cores": usable_cores,
        "reserved_cores": max(0, total_cores - usable_cores),
        "max_concurrency": concurrency,
        "queue_workers": max(1, min(concurrency, usable_cores)),
        "torch_num_threads": torch_threads,
        "torch_num_interop_threads": 1,
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

    async def apply_profile(self, profile: str) -> Dict[str, Any]:
        """Switch profile. Only concurrency changes; torch threads stay fixed."""
        normalized = (profile or "").strip().lower()
        if normalized not in SUPPORTED_PROFILES:
            raise ValidationError("Недопустимый профиль производительности")

        new_plan = _apply_settings_overrides(calculate_performance_plan(normalized))
        async with self._lock:
            for key in (
                "profile",
                "usable_cores",
                "reserved_cores",
                "max_concurrency",
                "queue_workers",
            ):
                self._plan[key] = new_plan[key]
            await self.limiter.resize(new_plan["max_concurrency"])
        return self.plan


runtime = RuntimePerformance()

