"""Server-side persistence for global runtime settings.

The database is the source of truth for the Whisper default model and the
performance profile: the UI changes them, so they must survive a restart, and
.env is not writable by the app. Values are cached in-process to keep the hot
path (enqueue, worker start) free of an extra query, and the cache is refreshed
on every write.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import AppSetting
from app.schemas.transcription import (
    DEFAULT_WHISPER_MODEL,
    normalize_whisper_model_name,
)

logger = logging.getLogger(__name__)

KEY_DEFAULT_MODEL = "whisper.default_model"
KEY_PROFILE = "whisper.performance_profile"

# Populated from the DB at startup so the worker never has to query per item.
_cache: dict[str, Any] = {}


async def load_all(db: AsyncSession) -> dict[str, Any]:
    """Read every setting into the in-process cache. Never raises."""
    try:
        result = await db.execute(select(AppSetting))
        _cache.clear()
        for row in result.scalars().all():
            _cache[row.key] = row.value
    except Exception as exc:
        # A missing table or an unreachable DB must not stop startup; the
        # defaults below keep the service usable.
        logger.error("[SETTINGS] Не удалось загрузить настройки: %s", exc)
    return dict(_cache)


async def get(db: AsyncSession, key: str, default: Any = None) -> Any:
    if key in _cache:
        return _cache[key]
    result = await db.execute(select(AppSetting).where(AppSetting.key == key))
    row = result.scalar_one_or_none()
    if row is None:
        return default
    _cache[key] = row.value
    return row.value


async def set_value(db: AsyncSession, key: str, value: Any) -> Any:
    """Upsert one setting and refresh the cache. Caller commits."""
    stmt = (
        pg_insert(AppSetting)
        .values(key=key, value=value)
        .on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": value})
    )
    await db.execute(stmt)
    _cache[key] = value
    return value


async def resolve_default_model(db: AsyncSession) -> str:
    """The default Whisper model for new work, from the persisted setting."""
    value = await get(db, KEY_DEFAULT_MODEL, DEFAULT_WHISPER_MODEL)
    # Guard against a hand-edited row holding an unsupported name.
    return normalize_whisper_model_name(value if isinstance(value, str) else None)


async def resolve_profile(db: AsyncSession) -> str:
    """The persisted performance profile, falling back to the .env default."""
    value = await get(db, KEY_PROFILE, get_settings().transcribe_performance_profile)
    return value if isinstance(value, str) else get_settings().transcribe_performance_profile


def cached(key: str, default: Any = None) -> Any:
    """Synchronous read for hot paths that cannot await (worker, planner)."""
    return _cache.get(key, default)
