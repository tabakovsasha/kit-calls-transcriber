"""Authentication routes: login, refresh, logout, session list, /me.

Token transport rules:
- the access token is returned in the response body and kept in memory by the
  SPA (never in localStorage);
- the refresh token only ever travels in an HttpOnly, SameSite cookie scoped
  to the refresh/logout paths, so XSS cannot read it and CSRF cannot reuse it
  on other endpoints.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Request, Response, status

from app.api.deps import (
    CurrentUserDep,
    SessionDep,
    get_client_ip,
    get_user_agent,
)
from app.core.config import get_settings
from app.core.errors import AuthError
from app.core.security import create_access_token
from app.db.models import User
from app.schemas.auth import (
    ChangePasswordRequest,
    CurrentUser,
    LoginRequest,
    SessionInfo,
    TokenResponse,
)
from app.schemas.common import MessageResponse
from app.services import auth_service, user_service
from app.services.audit_service import AuditAction, record_audit_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE_PATH = "/api/auth"


def _set_refresh_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=token,
        max_age=settings.refresh_token_ttl_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite=settings.refresh_cookie_samesite,
        path=REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite=settings.refresh_cookie_samesite,
        path=REFRESH_COOKIE_PATH,
    )


def _read_refresh_cookie(request: Request) -> Optional[str]:
    return request.cookies.get(get_settings().refresh_cookie_name)


def _token_response(user: User, session_id) -> TokenResponse:
    access_token, expires_in = create_access_token(
        user.id, user.email, user.role.value, session_id
    )
    return TokenResponse(
        access_token=access_token,
        expires_in=expires_in,
        user=CurrentUser.model_validate(user),
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: SessionDep,
) -> TokenResponse:
    ip_address = get_client_ip(request)
    user_agent = get_user_agent(request)

    user = await auth_service.authenticate(
        db,
        email=payload.email,
        password=payload.password,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    user_session, refresh_token = await auth_service.create_session(
        db, user, ip_address=ip_address, user_agent=user_agent
    )
    await record_audit_event(
        db,
        action=AuditAction.LOGIN_SUCCESS,
        actor_user_id=user.id,
        actor_email=user.email,
        owner_user_id=user.id,
        target_type="session",
        target_id=str(user_session.id),
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await db.commit()
    await db.refresh(user)

    _set_refresh_cookie(response, refresh_token)
    return _token_response(user, user_session.id)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(request: Request, response: Response, db: SessionDep) -> TokenResponse:
    """Rotate the refresh token and mint a new access token."""
    refresh_token = _read_refresh_cookie(request)
    if not refresh_token:
        raise AuthError("Отсутствует refresh-токен")

    try:
        user, user_session, new_refresh_token = await auth_service.rotate_session(
            db,
            refresh_token,
            ip_address=get_client_ip(request),
            user_agent=get_user_agent(request),
        )
    except AuthError:
        # The cookie is useless now: drop it so the SPA stops retrying.
        _clear_refresh_cookie(response)
        raise

    await record_audit_event(
        db,
        action=AuditAction.TOKEN_REFRESH,
        actor_user_id=user.id,
        actor_email=user.email,
        owner_user_id=user.id,
        target_type="session",
        target_id=str(user_session.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()

    _set_refresh_cookie(response, new_refresh_token)
    return _token_response(user, user_session.id)


@router.post("/logout", response_model=MessageResponse)
async def logout(request: Request, response: Response, db: SessionDep) -> MessageResponse:
    """Revoke the current session. Safe to call with an expired access token."""
    refresh_token = _read_refresh_cookie(request)
    if refresh_token:
        revoked = await auth_service.revoke_session_by_refresh_token(db, refresh_token)
        if revoked is not None:
            await record_audit_event(
                db,
                action=AuditAction.LOGOUT,
                actor_user_id=revoked.owner_user_id,
                owner_user_id=revoked.owner_user_id,
                target_type="session",
                target_id=str(revoked.id),
                ip_address=get_client_ip(request),
                user_agent=get_user_agent(request),
            )
        await db.commit()

    _clear_refresh_cookie(response)
    return MessageResponse(message="Сессия завершена")


@router.get("/me", response_model=CurrentUser)
async def get_me(user: CurrentUserDep) -> CurrentUser:
    return CurrentUser.model_validate(user)


@router.get("/sessions", response_model=list[SessionInfo])
async def list_sessions(user: CurrentUserDep, db: SessionDep) -> list[SessionInfo]:
    sessions = await auth_service.list_active_sessions(db, user.id)
    return [SessionInfo.model_validate(item) for item in sessions]


@router.post("/sessions/revoke-others", response_model=MessageResponse)
async def revoke_other_sessions(
    request: Request, user: CurrentUserDep, db: SessionDep
) -> MessageResponse:
    """Sign out every other device, keeping the current session alive."""
    current_session_id = getattr(request.state, "session_id", None)
    revoked = await auth_service.revoke_all_sessions(
        db, user.id, except_session_id=current_session_id
    )
    await record_audit_event(
        db,
        action=AuditAction.SESSIONS_REVOKED,
        actor_user_id=user.id,
        actor_email=user.email,
        owner_user_id=user.id,
        details={"revoked_count": revoked},
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    return MessageResponse(message=f"Завершено сессий: {revoked}")


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: CurrentUserDep,
    db: SessionDep,
) -> MessageResponse:
    """Change own password and revoke every other session."""
    await user_service.change_own_password(
        db,
        user,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    current_session_id = getattr(request.state, "session_id", None)
    await auth_service.revoke_all_sessions(db, user.id, except_session_id=current_session_id)
    await record_audit_event(
        db,
        action=AuditAction.PASSWORD_CHANGED,
        actor_user_id=user.id,
        actor_email=user.email,
        owner_user_id=user.id,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    return MessageResponse(message="Пароль изменен")
