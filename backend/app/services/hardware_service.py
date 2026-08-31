"""Hardware detection: CPU, RAM, GPU with container-awareness.

Detection must never crash the app. When precise values are unavailable, return
conservative estimates with a reason field explaining the source.
"""

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

try:
    import psutil  # type: ignore[import-not-found]
except ImportError:
    psutil = None

try:
    import torch
except ImportError:
    torch = None


def _read_cgroup_value(path: str) -> Optional[str]:
    """Read a cgroup v2 file, returning None if missing or unreadable."""
    try:
        return Path(path).read_text().strip()
    except Exception:
        return None


def _detect_cpu_limit() -> Optional[int]:
    """Return effective CPU count from cgroup quota, or None if unrestricted."""
    raw = _read_cgroup_value("/sys/fs/cgroup/cpu.max")
    if not raw or raw.startswith("max"):
        return None
    try:
        quota, period = raw.split()
        return max(1, int(int(quota) / int(period)))
    except Exception:
        return None


def _detect_effective_cpu_count() -> tuple[int, str]:
    """Return (count, source) where source explains how the value was derived."""
    try:
        affinity_count = len(os.sched_getaffinity(0))
        if affinity_count > 0:
            return affinity_count, "sched_getaffinity"
    except Exception:
        pass

    cgroup_limit = _detect_cpu_limit()
    if cgroup_limit is not None:
        return cgroup_limit, "cgroup_cpu_quota"

    logical = os.cpu_count() or 1
    return logical, "os_cpu_count"


def _detect_memory() -> Dict[str, Any]:
    """Detect total and available system memory in bytes."""
    if psutil:
        try:
            vm = psutil.virtual_memory()
            return {
                "total_bytes": vm.total,
                "available_bytes": vm.available,
                "source": "psutil",
            }
        except Exception as exc:
            logger.warning("[HARDWARE] psutil memory detection failed: %s", exc)

    try:
        meminfo = Path("/proc/meminfo").read_text()
        lines = {k: v for k, v in (line.split(":", 1) for line in meminfo.splitlines() if ":" in line)}
        total_kb = int(lines.get("MemTotal", "0").split()[0])
        avail_kb = int(lines.get("MemAvailable", "0").split()[0])
        if total_kb > 0:
            return {
                "total_bytes": total_kb * 1024,
                "available_bytes": avail_kb * 1024 if avail_kb > 0 else total_kb * 1024,
                "source": "proc_meminfo",
            }
    except Exception as exc:
        logger.warning("[HARDWARE] /proc/meminfo parsing failed: %s", exc)

    return {"total_bytes": 2 * 1024**3, "available_bytes": 1 * 1024**3, "source": "fallback"}


def _detect_gpu() -> Dict[str, Any]:
    """Detect GPU availability via torch.cuda/torch.backends.mps."""
    if torch is None:
        return {
            "available": False,
            "backend": None,
            "devices": [],
            "reason": "torch_not_installed",
        }

    if torch.cuda.is_available():
        devices = []
        try:
            for idx in range(torch.cuda.device_count()):
                props = torch.cuda.get_device_properties(idx)
                devices.append({
                    "index": idx,
                    "name": props.name,
                    "vram_bytes": props.total_memory,
                    "compute_capability": f"{props.major}.{props.minor}",
                })
        except Exception as exc:
            logger.warning("[HARDWARE] CUDA device enumeration failed: %s", exc)
        return {"available": True, "backend": "cuda", "devices": devices}

    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return {
            "available": True,
            "backend": "mps",
            "devices": [{"index": 0, "name": "Apple MPS", "vram_bytes": None}],
        }

    return {"available": False, "backend": None, "devices": [], "reason": "no_gpu_backend"}


def detect_runtime_capabilities() -> Dict[str, Any]:
    """Snapshot of available CPU, RAM and GPU resources."""
    try:
        effective_cpu, cpu_source = _detect_effective_cpu_count()
        logical_cpu = os.cpu_count() or 1
        physical_cpu = 1
        if psutil:
            try:
                physical_cpu = psutil.cpu_count(logical=False) or logical_cpu
            except Exception:
                physical_cpu = logical_cpu

        memory = _detect_memory()
        gpu = _detect_gpu()

        torch_info = {"installed": False, "version": None, "cuda_available": False, "mps_available": False}
        if torch is not None:
            torch_info = {
                "installed": True,
                "version": torch.__version__,
                "cuda_available": torch.cuda.is_available(),
                "mps_available": hasattr(torch.backends, "mps") and torch.backends.mps.is_available(),
            }

        return {
            "cpu": {
                "logical": logical_cpu,
                "physical": physical_cpu,
                "effective": effective_cpu,
                "source": cpu_source,
            },
            "memory": memory,
            "gpu": gpu,
            "torch": torch_info,
            "platform": {
                "system": platform.system(),
                "architecture": platform.machine(),
                "python_version": platform.python_version(),
            },
        }
    except Exception as exc:
        logger.error("[HARDWARE] Capability detection crashed: %s", exc, exc_info=True)
        return {
            "cpu": {"logical": 1, "physical": 1, "effective": 1, "source": "error_fallback"},
            "memory": {"total_bytes": 1 * 1024**3, "available_bytes": 512 * 1024**2, "source": "error_fallback"},
            "gpu": {"available": False, "backend": None, "devices": [], "reason": "detection_error"},
            "torch": {"installed": False, "version": None, "cuda_available": False, "mps_available": False},
            "platform": {"system": "unknown", "architecture": "unknown", "python_version": "unknown"},
            "error": str(exc),
        }
