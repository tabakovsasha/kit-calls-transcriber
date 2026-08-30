"""Recurring hourly transcription schedules, bound to one connection.

Replaces the legacy ``schedules.json``. Runs are idempotent per hourly slot:
``last_run_slot`` records the slot that already executed, so a restart or a
double poll cannot enqueue the same hour twice.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.security import utcnow
from app.core.timeparse import (
    API_DATETIME_FORMAT,
    is_hour_in_window,
    schedule_slot_key,
)
from app.db.models import TranscriptionSchedule
from app.schemas.transcription import normalize_whisper_model_name

logger = logging.getLogger(__name__)


async def list_schedules(
    db: AsyncSession, owner_user_id: uuid.UUID
) -> Sequence[TranscriptionSchedule]:
    result = await db.execute(
        select(TranscriptionSchedule)
        .where(TranscriptionSchedule.owner_user_id == owner_user_id)
        .order_by(TranscriptionSchedule.created_at.asc())
    )
    return result.scalars().all()


async def get_owned_schedule(
    db: AsyncSession, schedule_id: uuid.UUID, owner_user_id: uuid.UUID
) -> TranscriptionSchedule:
    result = await db.execute(
        select(TranscriptionSchedule).where(
            TranscriptionSchedule.id == schedule_id,
            TranscriptionSchedule.owner_user_id == owner_user_id,
        )
    )
    schedule = result.scalar_one_or_none()
    if schedule is None:
        raise NotFoundError("Расписание не найдено")
    return schedule


async def create_schedule(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    connection_id: uuid.UUID,
    name: str,
    weekdays: list[int],
    from_hour: int,
    to_hour: int,
    scenario_id: Optional[int],
    min_duration: int,
    has_recording: bool,
    records_limit: int,
    whisper_model: str,
    enabled: bool,
) -> TranscriptionSchedule:
    schedule = TranscriptionSchedule(
        owner_user_id=owner_user_id,
        connection_id=connection_id,
        name=(name or "").strip() or "Расписание транскрибации",
        weekdays=weekdays,
        from_hour=from_hour,
        to_hour=to_hour,
        scenario_id=scenario_id,
        min_duration=min_duration,
        has_recording=has_recording,
        records_limit=records_limit,
        whisper_model=normalize_whisper_model_name(whisper_model),
        enabled=enabled,
    )
    db.add(schedule)
    await db.flush()
    return schedule


async def update_schedule(
    db: AsyncSession,
    schedule: TranscriptionSchedule,
    *,
    name: Optional[str] = None,
    enabled: Optional[bool] = None,
    weekdays: Optional[list[int]] = None,
    from_hour: Optional[int] = None,
    to_hour: Optional[int] = None,
    min_duration: Optional[int] = None,
    has_recording: Optional[bool] = None,
    records_limit: Optional[int] = None,
    whisper_model: Optional[str] = None,
) -> TranscriptionSchedule:
    if name is not None:
        schedule.name = name.strip() or schedule.name
    if enabled is not None:
        schedule.enabled = enabled
    if weekdays is not None:
        cleaned = sorted({int(day) for day in weekdays if 0 <= int(day) <= 6})
        if cleaned:
            schedule.weekdays = cleaned
    if from_hour is not None:
        schedule.from_hour = from_hour
    if to_hour is not None:
        schedule.to_hour = to_hour
    if min_duration is not None:
        schedule.min_duration = min_duration
    if has_recording is not None:
        schedule.has_recording = has_recording
    if records_limit is not None:
        schedule.records_limit = records_limit
    if whisper_model is not None:
        schedule.whisper_model = normalize_whisper_model_name(whisper_model)

    await db.flush()
    return schedule


async def delete_schedule(db: AsyncSession, schedule: TranscriptionSchedule) -> None:
    await db.delete(schedule)
    await db.flush()


def is_due(schedule: TranscriptionSchedule, now: datetime) -> bool:
    """True when the schedule should run for the hour that just completed.

    Work always targets the *previous* full hour so the upstream has had time to
    finish writing recordings.
    """
    if not schedule.enabled:
        return False

    target = now - timedelta(hours=1)
    if target.weekday() not in (schedule.weekdays or []):
        return False
    if not is_hour_in_window(target.hour, schedule.from_hour, schedule.to_hour):
        return False

    return schedule.last_run_slot != schedule_slot_key(target)


def window_for(now: datetime) -> tuple[str, str, str]:
    """Return (from_value, to_value, slot_key) for the previous full hour."""
    target = now - timedelta(hours=1)
    start = target.replace(minute=0, second=0, microsecond=0)
    end = start.replace(minute=59, second=59)
    return (
        start.strftime(API_DATETIME_FORMAT),
        end.strftime(API_DATETIME_FORMAT),
        schedule_slot_key(target),
    )


async def due_schedules(db: AsyncSession, now: datetime) -> list[TranscriptionSchedule]:
    """All enabled schedules across all owners that are due for this slot."""
    result = await db.execute(
        select(TranscriptionSchedule).where(TranscriptionSchedule.enabled.is_(True))
    )
    return [schedule for schedule in result.scalars().all() if is_due(schedule, now)]


def record_run(
    schedule: TranscriptionSchedule, slot_key: str, result: dict[str, Any]
) -> None:
    """Stamp the outcome so the slot is not retried and the UI can show status."""
    schedule.last_run_at = utcnow()
    schedule.last_run_slot = slot_key
    schedule.last_result = result
