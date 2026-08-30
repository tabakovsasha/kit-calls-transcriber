"""Shared FastAPI dependencies: authentication, RBAC and owner scoping.

Equivalent of the reference project's JwtAuthGuard + RolesGuard, expressed as
dependencies:
- ``CurrentUserDep``   -> valid access token, active user, live session;
- ``AdminUserDep``     -> additionally requires the ADMIN role;
- ``resolve_owner_id`` -> admins may act on another user's data explicitly,
  regular users are always pinned to their own id.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Optional

import jwt
from fastapi import Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthError, ForbiddenError, NotFoundError
from app.core.security import decode_access_token, utcnow
from app.db.models import User, UserRole, UserSession
from app.db.session import get_session
from app.services.audit_service import AuditAction, record_audit_event

bearer_scheme = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_client_ip(request: Request) -> Optional[str]:
    """Best-effort client IP. Only trusted when running behind a known proxy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host[:64] if request.client else None


def get_user_agent(request: Request) -> Optional[str]:
    return (request.headers.get("user-agent") or "")[:512] or None


async def get_current_user(
    request: Request,
    db: SessionDep,
    credentials: Annotated[
        Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)
    ] = None,
) -> User:
    """Validate the bearer token and load the active user for this request."""
    if credentials is None or not credentials.credentials:
        raise AuthError("Требуется авторизация")

    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Срок действия токена истек", code="TOKEN_EXPIRED") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("Недействительный токен") from exc

    try:
        user_id = uuid.UUID(str(payload["sub"]))
        session_id = uuid.UUID(str(payload["sid"]))
    except (KeyError, ValueError) as exc:
        raise AuthError("Недействительный токен") from exc

    # The session must still be alive: logout and password change revoke
    # sessions, and an unexpired access token must not outlive them.
    result = await db.execute(
        select(UserSession).where(
            UserSession.id == session_id,
            UserSession.owner_user_id == user_id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > utcnow(),
        )
    )
    if result.scalar_one_or_none() is None:
        raise AuthError("Сессия завершена, войдите заново", code="SESSION_REVOKED")

    user = await db.get(User, user_id)
    if user is None:
        raise AuthError("Пользователь не найден")
    if not user.is_active:
        raise ForbiddenError("Учетная запись отключена")

    request.state.user = user
    request.state.session_id = session_id
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


async def get_current_admin(
    request: Request, db: SessionDep, user: CurrentUserDep
) -> User:
    if user.role != UserRole.ADMIN:
        await record_audit_event(
            db,
            action=AuditAction.ACCESS_DENIED,
            actor_user_id=user.id,
            actor_email=user.email,
            owner_user_id=user.id,
            severity="warning",
            target_type="endpoint",
            target_label=f"{request.method} {request.url.path}",
            ip_address=get_client_ip(request),
            user_agent=get_user_agent(request),
            details={"required_role": UserRole.ADMIN.value},
            commit=True,
        )
        raise ForbiddenError("Требуются права администратора")
    return user


AdminUserDep = Annotated[User, Depends(get_current_admin)]


async def get_password_change_exempt_user(user: CurrentUserDep) -> User:
    """Block normal API access while a forced password change is pending."""
    if user.must_change_password:
        raise ForbiddenError(
            "Необходимо сменить пароль перед продолжением работы",
            code="PASSWORD_CHANGE_REQUIRED",
        )
    return user


ActiveUserDep = Annotated[User, Depends(get_password_change_exempt_user)]


async def resolve_owner_id(
    db: SessionDep,
    user: ActiveUserDep,
    owner_user_id: Annotated[
        Optional[uuid.UUID],
        Query(description="Только для администраторов: работать от имени пользователя"),
    ] = None,
) -> uuid.UUID:
    """Return the owner id whose data the request may touch."""
    if owner_user_id is None or owner_user_id == user.id:
        return user.id

    if user.role != UserRole.ADMIN:
        raise ForbiddenError("Доступ только к своим данным")

    target = await db.get(User, owner_user_id)
    if target is None:
        raise NotFoundError("Пользователь не найден")
    return target.id


OwnerIdDep = Annotated[uuid.UUID, Depends(resolve_owner_id)]


def assert_self_or_admin(actor: User, target_user_id: uuid.UUID) -> None:
    """Guard for /users/{id} style routes."""
    if actor.id != target_user_id and actor.role != UserRole.ADMIN:
        raise ForbiddenError("Доступ только к своим данным")
