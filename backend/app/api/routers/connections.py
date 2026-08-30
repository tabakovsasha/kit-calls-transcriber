"""Voximplant connection management routes.

Every route resolves the owner through ``OwnerIdDep``, so a regular user only
ever touches their own connections while an admin can act on another user's
account by passing ?owner_user_id=. Access tokens are write-only: they enter
here and leave only as a mask plus fingerprint.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Request, status

from app.api.deps import (
    ActiveUserDep,
    OwnerIdDep,
    SessionDep,
    get_client_ip,
    get_user_agent,
)
from app.db.models import ConnectionStatus
from app.schemas.common import MessageResponse
from app.schemas.connections import (
    ConnectionCreateRequest,
    ConnectionResponse,
    ConnectionRotateTokenRequest,
    ConnectionUpdateRequest,
    ConnectionVerifyResponse,
)
from app.services import connection_service
from app.services.audit_service import AuditAction, record_audit_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connections", tags=["connections"])


@router.get("", response_model=list[ConnectionResponse])
async def list_connections(
    user: ActiveUserDep, owner_id: OwnerIdDep, db: SessionDep
) -> list[ConnectionResponse]:
    connections = await connection_service.list_connections(db, owner_id)
    return [connection_service.to_response(item) for item in connections]


@router.post("", response_model=ConnectionResponse, status_code=status.HTTP_201_CREATED)
async def create_connection(
    payload: ConnectionCreateRequest,
    request: Request,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> ConnectionResponse:
    connection = await connection_service.create_connection(
        db,
        owner_user_id=owner_id,
        label=payload.label,
        api_host=payload.api_host,
        domain=payload.domain,
        access_token=payload.access_token,
        is_default=payload.is_default,
        verify=payload.verify,
    )
    await record_audit_event(
        db,
        action=AuditAction.CONNECTION_CREATED,
        actor_user_id=user.id,
        actor_email=user.email,
        owner_user_id=owner_id,
        target_type="connection",
        target_id=str(connection.id),
        target_label=connection.label,
        # Only non-secret metadata is recorded; the token never appears.
        details={
            "api_host": connection.api_host,
            "domain": connection.domain,
            "verified": payload.verify,
            "status": connection.last_connection_status.value,
            "token_fingerprint": connection.access_token_fingerprint,
        },
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    await db.refresh(connection)
    return connection_service.to_response(connection)


@router.get("/{connection_id}", response_model=ConnectionResponse)
async def get_connection(
    connection_id: uuid.UUID,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> ConnectionResponse:
    connection = await connection_service.get_owned_connection(db, connection_id, owner_id)
    return connection_service.to_response(connection)


@router.put("/{connection_id}", response_model=ConnectionResponse)
async def update_connection(
    connection_id: uuid.UUID,
    payload: ConnectionUpdateRequest,
    request: Request,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> ConnectionResponse:
    connection = await connection_service.get_owned_connection(db, connection_id, owner_id)
    was_default = connection.is_default

    connection = await connection_service.update_connection(
        db,
        connection,
        label=payload.label,
        api_host=payload.api_host,
        domain=payload.domain,
        is_active=payload.is_active,
        is_default=payload.is_default,
    )
    await record_audit_event(
        db,
        action=(
            AuditAction.CONNECTION_DEFAULT_CHANGED
            if connection.is_default and not was_default
            else AuditAction.CONNECTION_UPDATED
        ),
        actor_user_id=user.id,
        actor_email=user.email,
        owner_user_id=owner_id,
        target_type="connection",
        target_id=str(connection.id),
        target_label=connection.label,
        details=payload.model_dump(exclude_none=True),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    await db.refresh(connection)
    return connection_service.to_response(connection)


@router.post("/{connection_id}/verify", response_model=ConnectionVerifyResponse)
async def verify_connection(
    connection_id: uuid.UUID,
    request: Request,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> ConnectionVerifyResponse:
    """Probe the stored credentials against the upstream API."""
    connection, credentials = await connection_service.resolve_credentials(
        db, connection_id, owner_id
    )
    connection = await connection_service.verify_connection(db, connection, credentials)
    await record_audit_event(
        db,
        action=AuditAction.CONNECTION_VERIFIED,
        actor_user_id=user.id,
        actor_email=user.email,
        owner_user_id=owner_id,
        target_type="connection",
        target_id=str(connection.id),
        target_label=connection.label,
        details={"status": connection.last_connection_status.value},
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    await db.refresh(connection)
    return ConnectionVerifyResponse(
        connection_id=connection.id,
        status=connection.last_connection_status,
        checked_at=connection.last_checked_at,
        error=connection.last_connection_error,
    )


@router.delete("/{connection_id}", response_model=MessageResponse)
async def delete_connection(
    connection_id: uuid.UUID,
    request: Request,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> MessageResponse:
    """Soft-delete a connection and scrub its encrypted token."""
    connection = await connection_service.get_owned_connection(db, connection_id, owner_id)
    label = connection.label
    fingerprint = connection.access_token_fingerprint
    await connection_service.soft_delete_connection(db, connection)
    await record_audit_event(
        db,
        action=AuditAction.CONNECTION_DELETED,
        actor_user_id=user.id,
        actor_email=user.email,
        owner_user_id=owner_id,
        severity="warning",
        target_type="connection",
        target_id=str(connection.id),
        target_label=label,
        details={"token_fingerprint": fingerprint},
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    return MessageResponse(message="Подключение удалено")


@router.post("/{connection_id}/rotate-token", response_model=ConnectionResponse)
async def rotate_connection_token(
    connection_id: uuid.UUID,
    payload: ConnectionRotateTokenRequest,
    request: Request,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> ConnectionResponse:
    """Replace the stored access token with a newly encrypted one."""
    connection = await connection_service.get_owned_connection(db, connection_id, owner_id)
    connection = await connection_service.rotate_token(
        db, connection, access_token=payload.access_token, verify=payload.verify
    )
    await record_audit_event(
        db,
        action=AuditAction.CONNECTION_TOKEN_ROTATED,
        actor_user_id=user.id,
        actor_email=user.email,
        owner_user_id=owner_id,
        severity="warning",
        target_type="connection",
        target_id=str(connection.id),
        target_label=connection.label,
        # Fingerprint lets an operator confirm which token is in use without
        # ever exposing the value itself.
        details={
            "verified": payload.verify,
            "status": connection.last_connection_status.value,
            "token_fingerprint": connection.access_token_fingerprint,
        },
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    await db.refresh(connection)
    return connection_service.to_response(connection)

