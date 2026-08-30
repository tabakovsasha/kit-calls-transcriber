"""Admin user management routes.

Every route here requires the ADMIN role (``AdminUserDep``), so a regular user
cannot enumerate accounts or change roles. Self-service equivalents live under
/auth and /profile.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Query, Request, status

from app.api.deps import (
    AdminUserDep,
    SessionDep,
    get_client_ip,
    get_user_agent,
)
from app.core.errors import ForbiddenError
from app.db.models import User, UserRole
from app.schemas.auth import ProfileSummary
from app.schemas.common import MessageResponse, Paginated
from app.schemas.users import (
    AdminResetPasswordRequest,
    ProfileUpdateRequest,
    UserCreateRequest,
    UserResponse,
    UserUpdateRequest,
)
from app.services import auth_service, connection_service, user_service
from app.services.audit_service import AuditAction, record_audit_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


def _to_response(user: User, connections_count: int = 0) -> UserResponse:
    payload = UserResponse.model_validate(user)
    payload.connections_count = connections_count
    return payload


@router.get("", response_model=Paginated[UserResponse])
async def list_users(
    admin: AdminUserDep,
    db: SessionDep,
    search: Annotated[Optional[str], Query(max_length=200)] = None,
    role: Annotated[Optional[UserRole], Query()] = None,
    is_active: Annotated[Optional[bool], Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Paginated[UserResponse]:
    users, total, counts = await user_service.list_users(
        db, search=search, role=role, is_active=is_active, page=page, limit=limit
    )
    return Paginated[UserResponse](
        items=[_to_response(item, counts.get(item.id, 0)) for item in users],
        total=total,
        page=page,
        limit=limit,
    )


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateRequest,
    request: Request,
    admin: AdminUserDep,
    db: SessionDep,
) -> UserResponse:
    user = await user_service.create_user(
        db,
        email=payload.email,
        password=payload.password,
        role=payload.role,
        is_active=payload.is_active,
        must_change_password=payload.must_change_password,
        first_name=payload.first_name,
        last_name=payload.last_name,
    )
    await record_audit_event(
        db,
        action=AuditAction.USER_CREATED,
        actor_user_id=admin.id,
        actor_email=admin.email,
        owner_user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        target_label=user.email,
        # Password intentionally omitted: audit payloads never carry secrets.
        details={"role": user.role.value, "is_active": user.is_active},
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    await db.refresh(user)
    return _to_response(user)


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(user_id: uuid.UUID, admin: AdminUserDep, db: SessionDep) -> UserResponse:
    user = await user_service.get_user(db, user_id)
    count = await connection_service.count_connections(db, user.id)
    return _to_response(user, count)


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdateRequest,
    request: Request,
    admin: AdminUserDep,
    db: SessionDep,
) -> UserResponse:
    user = await user_service.get_user(db, user_id)
    user, changes = await user_service.update_user(
        db,
        user,
        actor=admin,
        email=payload.email,
        role=payload.role,
        is_active=payload.is_active,
        must_change_password=payload.must_change_password,
    )

    # A revoked role or a disabled account must not keep working sessions.
    if "role" in changes or changes.get("is_active") is not None:
        await auth_service.revoke_all_sessions(db, user.id)

    action = AuditAction.USER_UPDATED
    if "role" in changes:
        action = AuditAction.USER_ROLE_CHANGED
    elif "is_active" in changes:
        action = (
            AuditAction.USER_ACTIVATED if user.is_active else AuditAction.USER_DEACTIVATED
        )

    if changes:
        await record_audit_event(
            db,
            action=action,
            actor_user_id=admin.id,
            actor_email=admin.email,
            owner_user_id=user.id,
            severity="warning" if action == AuditAction.USER_ROLE_CHANGED else "info",
            target_type="user",
            target_id=str(user.id),
            target_label=user.email,
            details=changes,
            ip_address=get_client_ip(request),
            user_agent=get_user_agent(request),
        )

    await db.commit()
    await db.refresh(user)
    count = await connection_service.count_connections(db, user.id)
    return _to_response(user, count)


@router.put("/{user_id}/profile", response_model=ProfileSummary)
async def update_user_profile(
    user_id: uuid.UUID,
    payload: ProfileUpdateRequest,
    request: Request,
    admin: AdminUserDep,
    db: SessionDep,
) -> ProfileSummary:
    user = await user_service.get_user(db, user_id)
    profile = await user_service.upsert_profile(
        db,
        user,
        first_name=payload.first_name,
        last_name=payload.last_name,
        display_name=payload.display_name,
        timezone=payload.timezone,
        language=payload.language,
        avatar_url=payload.avatar_url,
    )
    await record_audit_event(
        db,
        action=AuditAction.PROFILE_UPDATED,
        actor_user_id=admin.id,
        actor_email=admin.email,
        owner_user_id=user.id,
        target_type="profile",
        target_id=str(profile.id),
        target_label=user.email,
        details=payload.model_dump(exclude_none=True),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    await db.refresh(profile)
    return ProfileSummary.model_validate(profile)


@router.post("/{user_id}/reset-password", response_model=MessageResponse)
async def reset_user_password(
    user_id: uuid.UUID,
    payload: AdminResetPasswordRequest,
    request: Request,
    admin: AdminUserDep,
    db: SessionDep,
) -> MessageResponse:
    """Set a new password for another user and terminate their sessions."""
    user = await user_service.get_user(db, user_id)
    await user_service.admin_reset_password(
        db,
        user,
        new_password=payload.new_password,
        must_change_password=payload.must_change_password,
    )
    revoked = await auth_service.revoke_all_sessions(db, user.id)
    await record_audit_event(
        db,
        action=AuditAction.PASSWORD_RESET_BY_ADMIN,
        actor_user_id=admin.id,
        actor_email=admin.email,
        owner_user_id=user.id,
        severity="warning",
        target_type="user",
        target_id=str(user.id),
        target_label=user.email,
        # The new password itself is never recorded.
        details={
            "must_change_password": payload.must_change_password,
            "revoked_sessions": revoked,
        },
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    return MessageResponse(message="Пароль пользователя обновлен")


@router.post("/{user_id}/revoke-sessions", response_model=MessageResponse)
async def revoke_user_sessions(
    user_id: uuid.UUID,
    request: Request,
    admin: AdminUserDep,
    db: SessionDep,
) -> MessageResponse:
    user = await user_service.get_user(db, user_id)
    revoked = await auth_service.revoke_all_sessions(db, user.id)
    await record_audit_event(
        db,
        action=AuditAction.SESSIONS_REVOKED,
        actor_user_id=admin.id,
        actor_email=admin.email,
        owner_user_id=user.id,
        severity="warning",
        target_type="user",
        target_id=str(user.id),
        target_label=user.email,
        details={"revoked_count": revoked},
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    return MessageResponse(message=f"Завершено сессий: {revoked}")


@router.delete("/{user_id}", response_model=MessageResponse)
async def deactivate_user(
    user_id: uuid.UUID,
    request: Request,
    admin: AdminUserDep,
    db: SessionDep,
) -> MessageResponse:
    """Deactivate an account.

    Deliberately not a hard delete: transcripts and audit history must stay
    attributable. Deactivation revokes all sessions immediately.
    """
    if user_id == admin.id:
        raise ForbiddenError("Нельзя отключить собственную учетную запись")

    user = await user_service.get_user(db, user_id)
    user, changes = await user_service.update_user(db, user, actor=admin, is_active=False)
    revoked = await auth_service.revoke_all_sessions(db, user.id)
    await record_audit_event(
        db,
        action=AuditAction.USER_DEACTIVATED,
        actor_user_id=admin.id,
        actor_email=admin.email,
        owner_user_id=user.id,
        severity="warning",
        target_type="user",
        target_id=str(user.id),
        target_label=user.email,
        details={"revoked_sessions": revoked, **changes},
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    return MessageResponse(message="Учетная запись отключена")
