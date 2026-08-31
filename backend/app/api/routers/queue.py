"""Transcription queue routes.

Everything is owner-scoped through ``OwnerIdDep``: a user only ever sees and
mutates their own queue. Clients send call ids only; the record URL needed to
fetch audio is resolved server-side from the search cache, so a browser can
never inject an upstream URL.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Request

from app.api.deps import (
    ActiveUserDep,
    OwnerIdDep,
    SessionDep,
    get_client_ip,
    get_user_agent,
)
from app.core.errors import NotFoundError, ValidationError
from app.db.models import QueueItemStatus
from app.schemas.common import MessageResponse
from app.schemas.transcription import (
    QueueAddRequest,
    QueueAddResponse,
    QueueClearRequest,
    QueueSnapshot,
)
from app.services import call_cache, connection_service, queue_service
from app.services import app_settings_service
from app.services.audit_service import AuditAction, record_audit_event
from app.services.events_service import hub
from app.services import worker_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/queue", tags=["queue"])


async def _publish_snapshot(owner_id: uuid.UUID, snapshot: QueueSnapshot) -> None:
    """Push the current queue state to live sockets. Never raises."""
    try:
        await hub.publish(
            owner_id, {"type": "queue_snapshot", "snapshot": snapshot.model_dump(mode="json")}
        )
    except Exception as exc:
        logger.warning("[QUEUE] Snapshot broadcast failed for owner=%s: %s", owner_id, exc)


@router.get("", response_model=QueueSnapshot)
async def get_queue(
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
    connection_id: Optional[uuid.UUID] = None,
) -> QueueSnapshot:
    return await queue_service.build_snapshot(
        db, owner_id, connection_id=connection_id, is_running=worker_service.is_running()
    )


@router.post("/add", response_model=QueueAddResponse)
async def add_to_queue(
    payload: QueueAddRequest,
    request: Request,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> QueueAddResponse:
    """Enqueue calls for transcription.

    Metadata is taken from the server-side search cache. A call whose metadata
    expired is rejected with a clear instruction rather than being queued into a
    guaranteed failure.
    """
    connection = await connection_service.get_owned_connection(
        db, payload.connection_id, owner_id
    )

    metadata_by_call: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for call_id in payload.call_ids:
        cached = call_cache.lookup(owner_id, connection.id, call_id)
        if cached and cached.get("record_url"):
            metadata_by_call[call_id] = cached
        else:
            missing.append(call_id)

    if not metadata_by_call:
        raise ValidationError(
            "Данные о звонках устарели. Повторите поиск и добавьте звонки заново"
        )

    # An omitted model means "whatever the admin configured", resolved here so
    # the queue row records the concrete name it was enqueued with.
    whisper_model = payload.whisper_model or await app_settings_service.resolve_default_model(db)

    created, skipped_active = await queue_service.add_items(
        db,
        owner_user_id=owner_id,
        connection_id=connection.id,
        call_ids=list(metadata_by_call.keys()),
        whisper_model=whisper_model,
        metadata_by_call=metadata_by_call,
    )
    await record_audit_event(
        db,
        action=AuditAction.QUEUE_ITEM_ADDED,
        actor_user_id=user.id,
        actor_email=user.email,
        owner_user_id=owner_id,
        target_type="connection",
        target_id=str(connection.id),
        target_label=connection.label,
        details={
            "queued": len(created),
            "already_active": len(skipped_active),
            "expired_metadata": len(missing),
            "whisper_model": payload.whisper_model,
        },
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()

    snapshot = await queue_service.build_snapshot(
        db, owner_id, is_running=worker_service.is_running()
    )
    await _publish_snapshot(owner_id, snapshot)
    return QueueAddResponse(
        queued=len(created),
        skipped_active=skipped_active,
        skipped_expired=missing,
        snapshot=snapshot,
    )


@router.post("/retry", response_model=QueueSnapshot)
async def retry_queue(
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> QueueSnapshot:
    """Requeue failed and skipped items."""
    await queue_service.requeue_by_status(
        db, owner_id, [QueueItemStatus.FAILED, QueueItemStatus.SKIPPED]
    )
    await db.commit()
    snapshot = await queue_service.build_snapshot(
        db, owner_id, is_running=worker_service.is_running()
    )
    await _publish_snapshot(owner_id, snapshot)
    return snapshot


@router.post("/stop", response_model=QueueSnapshot)
async def stop_queue(
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> QueueSnapshot:
    """Cancel everything still waiting. Items mid-inference finish on their own."""
    await queue_service.cancel_active(db, owner_id)
    await db.commit()
    snapshot = await queue_service.build_snapshot(
        db, owner_id, is_running=worker_service.is_running()
    )
    await _publish_snapshot(owner_id, snapshot)
    return snapshot


@router.post("/clear", response_model=QueueSnapshot)
async def clear_queue(
    payload: QueueClearRequest,
    request: Request,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> QueueSnapshot:
    """Delete terminal items. Cached transcripts are kept."""
    removed = await queue_service.clear_by_status(db, owner_id, payload.statuses)
    await record_audit_event(
        db,
        action=AuditAction.QUEUE_CLEARED,
        actor_user_id=user.id,
        actor_email=user.email,
        owner_user_id=owner_id,
        target_type="queue",
        details={
            "removed": removed,
            "statuses": [status.value for status in payload.statuses],
        },
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    snapshot = await queue_service.build_snapshot(
        db, owner_id, is_running=worker_service.is_running()
    )
    await _publish_snapshot(owner_id, snapshot)
    return snapshot


@router.post("/{item_id}/cancel", response_model=QueueSnapshot)
async def cancel_one_item(
    item_id: uuid.UUID,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> QueueSnapshot:
    """Cancel one queued item. Processing items finish on their own."""
    item = await queue_service.get_owned_item(db, item_id, owner_id)
    if item is None:
        raise NotFoundError("Элемент очереди не найден")
    if not queue_service.can_cancel(item):
        raise ValidationError("Нельзя отменить элемент, который уже обрабатывается или завершён")

    queue_service.cancel_item(item)
    await db.commit()
    snapshot = await queue_service.build_snapshot(
        db, owner_id, is_running=worker_service.is_running()
    )
    await _publish_snapshot(owner_id, snapshot)
    return snapshot


@router.post("/{item_id}/retry", response_model=QueueSnapshot)
async def retry_one_item(
    item_id: uuid.UUID,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> QueueSnapshot:
    """Requeue one failed/canceled/skipped item."""
    item = await queue_service.get_owned_item(db, item_id, owner_id)
    if item is None:
        raise NotFoundError("Элемент очереди не найден")
    if not queue_service.can_retry(item):
        raise ValidationError("Этот элемент нельзя повторить (уже активен или готов)")

    queue_service.retry_item(item)
    await db.commit()
    snapshot = await queue_service.build_snapshot(
        db, owner_id, is_running=worker_service.is_running()
    )
    await _publish_snapshot(owner_id, snapshot)
    return snapshot


@router.delete("/{item_id}", response_model=MessageResponse)
async def delete_queue_item(
    item_id: uuid.UUID,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> MessageResponse:
    item = await queue_service.get_owned_item(db, item_id, owner_id)
    if item is None:
        raise NotFoundError("Элемент очереди не найден")
    if item.status == QueueItemStatus.PROCESSING:
        raise ValidationError("Нельзя удалить элемент, который обрабатывается")

    await queue_service.delete_item(db, item)
    await db.commit()
    return MessageResponse(message="Элемент очереди удален")
