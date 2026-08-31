"""Durable transcription queue, per owner and per connection.

Replaces the legacy ``transcription-queue.json``. Every read and write is scoped
by owner_user_id, so one user's queue is invisible to another. Upstream record
URLs live in the row but never in a response payload.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import utcnow
from app.db.models import QueueItemStatus, TranscriptionQueueItem
from app.schemas.transcription import QueueCounters, QueueItemResponse, QueueSnapshot
from app.services import media_service

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = (QueueItemStatus.QUEUED, QueueItemStatus.PROCESSING)


def to_response(
    item: TranscriptionQueueItem, *, owner_user_id: Optional[uuid.UUID] = None
) -> QueueItemResponse:
    """Serialize a queue row, replacing the upstream URL with a signed one."""
    response = QueueItemResponse.model_validate(item)
    if item.status == QueueItemStatus.DONE and item.record_url:
        owner = owner_user_id or item.owner_user_id
        response.audio_url, _ = media_service.build_audio_url(
            owner, item.connection_id, item.call_id
        )
    return response


async def list_items(
    db: AsyncSession,
    owner_user_id: uuid.UUID,
    *,
    connection_id: Optional[uuid.UUID] = None,
    statuses: Optional[Sequence[QueueItemStatus]] = None,
    limit: int = 1000,
) -> Sequence[TranscriptionQueueItem]:
    stmt = select(TranscriptionQueueItem).where(
        TranscriptionQueueItem.owner_user_id == owner_user_id
    )
    if connection_id is not None:
        stmt = stmt.where(TranscriptionQueueItem.connection_id == connection_id)
    if statuses:
        stmt = stmt.where(TranscriptionQueueItem.status.in_(list(statuses)))
    result = await db.execute(
        stmt.order_by(TranscriptionQueueItem.created_at.asc()).limit(limit)
    )
    return result.scalars().all()


async def get_owned_item(
    db: AsyncSession, item_id: uuid.UUID, owner_user_id: uuid.UUID
) -> Optional[TranscriptionQueueItem]:
    result = await db.execute(
        select(TranscriptionQueueItem).where(
            TranscriptionQueueItem.id == item_id,
            TranscriptionQueueItem.owner_user_id == owner_user_id,
        )
    )
    return result.scalar_one_or_none()


async def count_by_status(
    db: AsyncSession,
    owner_user_id: uuid.UUID,
    *,
    connection_id: Optional[uuid.UUID] = None,
) -> QueueCounters:
    stmt = (
        select(TranscriptionQueueItem.status, func.count())
        .where(TranscriptionQueueItem.owner_user_id == owner_user_id)
        .group_by(TranscriptionQueueItem.status)
    )
    if connection_id is not None:
        stmt = stmt.where(TranscriptionQueueItem.connection_id == connection_id)

    result = await db.execute(stmt)
    counters = QueueCounters()
    for status, count in result.all():
        value = int(count)
        counters.total += value
        setattr(counters, status.value, value)
    return counters


async def add_items(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    connection_id: uuid.UUID,
    call_ids: Iterable[str],
    whisper_model: str,
    metadata_by_call: dict[str, dict[str, Any]],
) -> tuple[list[TranscriptionQueueItem], list[str]]:
    """Enqueue calls idempotently.

    Returns (created_or_requeued, skipped_call_ids). A call already queued or
    processing is left alone; a finished one is reset so it can run again.
    """
    requested = [call_id for call_id in {str(item) for item in call_ids} if call_id]
    if not requested:
        return [], []

    existing_result = await db.execute(
        select(TranscriptionQueueItem).where(
            TranscriptionQueueItem.owner_user_id == owner_user_id,
            TranscriptionQueueItem.connection_id == connection_id,
            TranscriptionQueueItem.call_id.in_(requested),
        )
    )
    existing = {item.call_id: item for item in existing_result.scalars().all()}

    touched: list[TranscriptionQueueItem] = []
    skipped: list[str] = []

    for call_id in requested:
        metadata = metadata_by_call.get(call_id) or {}
        item = existing.get(call_id)

        if item is not None and item.status in ACTIVE_STATUSES:
            skipped.append(call_id)
            continue

        if item is None:
            item = TranscriptionQueueItem(
                owner_user_id=owner_user_id,
                connection_id=connection_id,
                call_id=call_id,
            )
            db.add(item)

        item.status = QueueItemStatus.QUEUED
        item.whisper_model = whisper_model
        item.error_message = None
        item.transcript_text = None
        item.started_at = None
        item.finished_at = None
        # Metadata may be absent when the search cache expired; the worker will
        # then fail the item with a clear message instead of guessing a URL.
        if metadata:
            item.record_url = metadata.get("record_url")
            item.datetime_start = metadata.get("datetime_start")
            item.timezone = metadata.get("timezone")
            item.caller_a = metadata.get("phone_a")
            item.caller_b = metadata.get("phone_b")
            item.duration = metadata.get("duration")
            item.scenario_name = metadata.get("scenario_name")
        touched.append(item)

    await db.flush()
    return touched, skipped


async def build_snapshot(
    db: AsyncSession,
    owner_user_id: uuid.UUID,
    *,
    connection_id: Optional[uuid.UUID] = None,
    is_running: bool = False,
) -> QueueSnapshot:
    """Full queue state for one owner, safe to send over the websocket."""
    items = await list_items(db, owner_user_id, connection_id=connection_id)
    counters = await count_by_status(db, owner_user_id, connection_id=connection_id)
    return QueueSnapshot(
        items=[to_response(item, owner_user_id=owner_user_id) for item in items],
        counters=counters,
        is_running=is_running,
    )


async def claim_next_item(
    db: AsyncSession, owner_user_id: uuid.UUID
) -> Optional[TranscriptionQueueItem]:
    """Atomically take the oldest queued item for this owner.

    ``FOR UPDATE SKIP LOCKED`` lets several workers drain the same queue without
    ever handing the same call to two of them.
    """
    result = await db.execute(
        select(TranscriptionQueueItem)
        .where(
            TranscriptionQueueItem.owner_user_id == owner_user_id,
            TranscriptionQueueItem.status == QueueItemStatus.QUEUED,
        )
        .order_by(TranscriptionQueueItem.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    item = result.scalar_one_or_none()
    if item is None:
        return None

    item.status = QueueItemStatus.PROCESSING
    item.started_at = utcnow()
    item.attempts += 1
    item.error_message = None
    await db.flush()
    return item


async def owners_with_pending_work(db: AsyncSession) -> Sequence[uuid.UUID]:
    """Owners that currently have queued items, used by the background runner."""
    result = await db.execute(
        select(TranscriptionQueueItem.owner_user_id)
        .where(TranscriptionQueueItem.status == QueueItemStatus.QUEUED)
        .group_by(TranscriptionQueueItem.owner_user_id)
    )
    return [row[0] for row in result.all()]


def mark_done(item: TranscriptionQueueItem, transcript_text: str) -> None:
    item.status = QueueItemStatus.DONE
    item.transcript_text = transcript_text
    item.error_message = None
    item.finished_at = utcnow()


def mark_skipped(item: TranscriptionQueueItem, reason: str) -> None:
    item.status = QueueItemStatus.SKIPPED
    item.transcript_text = ""
    item.error_message = reason
    item.finished_at = utcnow()


def mark_failed(item: TranscriptionQueueItem, reason: str) -> None:
    item.status = QueueItemStatus.FAILED
    item.error_message = reason[:2000]
    item.finished_at = utcnow()


async def requeue_by_status(
    db: AsyncSession,
    owner_user_id: uuid.UUID,
    statuses: Sequence[QueueItemStatus],
) -> int:
    """Reset failed/skipped items back to queued. Returns how many were reset."""
    items = await list_items(db, owner_user_id, statuses=statuses)
    for item in items:
        item.status = QueueItemStatus.QUEUED
        item.error_message = None
        item.transcript_text = None
        item.started_at = None
        item.finished_at = None
    await db.flush()
    return len(items)


CANCELABLE_STATUSES = (QueueItemStatus.QUEUED,)
RETRYABLE_STATUSES = (
    QueueItemStatus.FAILED,
    QueueItemStatus.SKIPPED,
    QueueItemStatus.CANCELED,
)


def can_cancel(item: TranscriptionQueueItem) -> bool:
    """Only waiting items can be cancelled.

    An item mid-inference is deliberately excluded: Whisper runs inside a
    thread and there is no safe way to abort it, so the honest answer is that
    it must finish. Nothing here ever tries to kill a running model.
    """
    return item.status in CANCELABLE_STATUSES


def can_retry(item: TranscriptionQueueItem) -> bool:
    return item.status in RETRYABLE_STATUSES


def cancel_item(item: TranscriptionQueueItem) -> None:
    """Cancel one waiting item. Caller checked ``can_cancel``."""
    item.status = QueueItemStatus.CANCELED
    item.error_message = None
    item.finished_at = utcnow()


def retry_item(item: TranscriptionQueueItem) -> None:
    """Send one terminal item back to the queue.

    ``attempts`` is intentionally left as-is: it is a lifetime counter of how
    many times this call was actually handed to the worker, and resetting it
    would hide a call that keeps failing. The claim step increments it again.
    """
    item.status = QueueItemStatus.QUEUED
    item.error_message = None
    item.transcript_text = None
    item.started_at = None
    item.finished_at = None


async def cancel_active(db: AsyncSession, owner_user_id: uuid.UUID) -> int:
    """Cancel everything still waiting. Items mid-inference finish on their own."""
    items = await list_items(db, owner_user_id, statuses=[QueueItemStatus.QUEUED])
    for item in items:
        item.status = QueueItemStatus.CANCELED
        item.finished_at = utcnow()
    await db.flush()
    return len(items)


async def clear_by_status(
    db: AsyncSession,
    owner_user_id: uuid.UUID,
    statuses: Sequence[QueueItemStatus],
) -> int:
    """Delete terminal items. Transcripts survive in their own table."""
    safe_statuses = [status for status in statuses if status not in ACTIVE_STATUSES]
    if not safe_statuses:
        return 0

    result = await db.execute(
        delete(TranscriptionQueueItem).where(
            TranscriptionQueueItem.owner_user_id == owner_user_id,
            TranscriptionQueueItem.status.in_(safe_statuses),
        )
    )
    return int(result.rowcount or 0)


async def delete_item(
    db: AsyncSession, item: TranscriptionQueueItem
) -> None:
    await db.delete(item)
    await db.flush()

