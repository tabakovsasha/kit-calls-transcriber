"""Recurring transcription schedule routes.

Owner-scoped through ``OwnerIdDep``. A schedule is always bound to a connection
the caller owns, and the run itself is executed by the background worker so the
request does not block on upstream paging or inference.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request, status

from app.api.deps import (
    ActiveUserDep,
    OwnerIdDep,
    SessionDep,
    get_client_ip,
    get_user_agent,
)
from app.core.errors import ValidationError
from app.schemas.common import MessageResponse
from app.schemas.transcription import (
    ScheduleCreateRequest,
    ScheduleResponse,
    ScheduleUpdateRequest,
)
from app.services import connection_service, schedule_service, worker_service
from app.services.audit_service import AuditAction, record_audit_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schedules", tags=["schedules"])


@router.get("", response_model=list[ScheduleResponse])
async def list_schedules(
    user: ActiveUserDep, owner_id: OwnerIdDep, db: SessionDep
) -> list[ScheduleResponse]:
    schedules = await schedule_service.list_schedules(db, owner_id)
    return [ScheduleResponse.model_validate(item) for item in schedules]


@router.post("", response_model=ScheduleResponse, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    payload: ScheduleCreateRequest,
    request: Request,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> ScheduleResponse:
    # Ownership of the connection is verified before anything is persisted.
    connection = await connection_service.get_owned_connection(
        db, payload.connection_id, owner_id
    )

    schedule = await schedule_service.create_schedule(
        db,
        owner_user_id=owner_id,
        connection_id=connection.id,
        name=payload.name,
        weekdays=payload.weekdays,
        from_hour=payload.from_hour,
        to_hour=payload.to_hour,
        scenario_id=payload.scenario_id,
        min_duration=payload.min_duration,
        has_recording=payload.has_recording,
        records_limit=payload.records_limit,
        whisper_model=payload.whisper_model,
        enabled=payload.enabled,
    )
    await record_audit_event(
        db,
        action=AuditAction.SCHEDULE_CREATED,
        actor_user_id=user.id,
        actor_email=user.email,
        owner_user_id=owner_id,
        target_type="schedule",
        target_id=str(schedule.id),
        target_label=schedule.name,
        details={
            "connection_id": str(connection.id),
            "weekdays": schedule.weekdays,
            "from_hour": schedule.from_hour,
            "to_hour": schedule.to_hour,
            "whisper_model": schedule.whisper_model,
            "enabled": schedule.enabled,
        },
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    await db.refresh(schedule)
    return ScheduleResponse.model_validate(schedule)


@router.get("/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule(
    schedule_id: uuid.UUID,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> ScheduleResponse:
    schedule = await schedule_service.get_owned_schedule(db, schedule_id, owner_id)
    return ScheduleResponse.model_validate(schedule)


@router.put("/{schedule_id}", response_model=ScheduleResponse)
async def update_schedule(
    schedule_id: uuid.UUID,
    payload: ScheduleUpdateRequest,
    request: Request,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> ScheduleResponse:
    schedule = await schedule_service.get_owned_schedule(db, schedule_id, owner_id)
    schedule = await schedule_service.update_schedule(
        db,
        schedule,
        name=payload.name,
        enabled=payload.enabled,
        weekdays=payload.weekdays,
        from_hour=payload.from_hour,
        to_hour=payload.to_hour,
        min_duration=payload.min_duration,
        has_recording=payload.has_recording,
        records_limit=payload.records_limit,
        whisper_model=payload.whisper_model,
    )
    await record_audit_event(
        db,
        action=AuditAction.SCHEDULE_UPDATED,
        actor_user_id=user.id,
        actor_email=user.email,
        owner_user_id=owner_id,
        target_type="schedule",
        target_id=str(schedule.id),
        target_label=schedule.name,
        details=payload.model_dump(exclude_none=True),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    await db.refresh(schedule)
    return ScheduleResponse.model_validate(schedule)


@router.delete("/{schedule_id}", response_model=MessageResponse)
async def delete_schedule(
    schedule_id: uuid.UUID,
    request: Request,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> MessageResponse:
    schedule = await schedule_service.get_owned_schedule(db, schedule_id, owner_id)
    label = schedule.name
    await schedule_service.delete_schedule(db, schedule)
    await record_audit_event(
        db,
        action=AuditAction.SCHEDULE_DELETED,
        actor_user_id=user.id,
        actor_email=user.email,
        owner_user_id=owner_id,
        severity="warning",
        target_type="schedule",
        target_id=str(schedule_id),
        target_label=label,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    return MessageResponse(message="Расписание удалено")


@router.post("/{schedule_id}/run-now")
async def run_schedule_now(
    schedule_id: uuid.UUID,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> dict[str, Any]:
    """Execute a schedule immediately for the previous full hour."""
    schedule = await schedule_service.get_owned_schedule(db, schedule_id, owner_id)
    if not schedule.enabled:
        raise ValidationError("Расписание отключено")

    # The worker opens its own session, so release this one first.
    await db.commit()
    return await worker_service.run_schedule_now(schedule.id, owner_id)
