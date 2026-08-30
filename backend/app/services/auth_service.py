"""Authentication service: login, refresh-token rotation, session lifecycle.

Security properties:
- refresh tokens are opaque, stored only as SHA-256 hashes, single-use
  (rotated on every refresh);
- reuse of an already-rotated token revokes the whole session family;
- login failures are rate limited per user and counted for lockout;
- error messages never reveal whether an email exists.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AuthError, ForbiddenError
from app.core.security import (
    generate_refresh_token,
    hash_refresh_token,
    hash_password,
    needs_rehash,
    refresh_token_expiry,
    utcnow,
    verify_password,
)
from app.db.models import User, UserProfile, UserSession
from app.services.audit_service import AuditAction, record_audit_event

logger = logging.getLogger(__name__)

INVALID_CREDENTIALS_MESSAGE = "Неверный email или пароль"
LOCKOUT_THRESHOLD = 10
LOCKOUT_DURATION_MINUTES = 15


async def get_user_by_email(session: AsyncSession, email: str) -> Optional[User]:
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    result = await session.execute(select(User).where(func.lower(User.email) == normalized))
    return result.scalar_one_or_none()


async def _count_recent_failures(session: AsyncSession, email: str) -> int:
    settings = get_settings()
    from app.db.models import AuditEvent

    window_start = utcnow() - timedelta(seconds=settings.login_rate_limit_window_seconds)
    result = await session.execute(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.action == AuditAction.LOGIN_FAILED,
            AuditEvent.actor_email == email.strip().lower(),
            AuditEvent.created_at >= window_start,
        )
    )
    return int(result.scalar() or 0)


async def authenticate(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    ip_address: Optional[str],
    user_agent: Optional[str],
) -> User:
    """Verify credentials. Raises AuthError with a generic message on failure."""
    settings = get_settings()
    normalized_email = (email or "").strip().lower()

    recent_failures = await _count_recent_failures(session, normalized_email)
    if recent_failures >= settings.login_rate_limit_attempts:
        await record_audit_event(
            session,
            action=AuditAction.LOGIN_BLOCKED,
            actor_email=normalized_email,
            severity="warning",
            ip_address=ip_address,
            user_agent=user_agent,
            details={"recent_failures": recent_failures},
            commit=True,
        )
        raise AuthError("Слишком много попыток входа. Повторите позже.", code="RATE_LIMITED")

    user = await get_user_by_email(session, normalized_email)

    # Always run a hash comparison to keep timing roughly uniform.
    stored_hash = user.password_hash if user else hash_password(generate_refresh_token())
    password_ok = verify_password(stored_hash, password)

    if not user or not password_ok:
        if user:
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= LOCKOUT_THRESHOLD:
                user.locked_until = utcnow() + timedelta(minutes=LOCKOUT_DURATION_MINUTES)
        await record_audit_event(
            session,
            action=AuditAction.LOGIN_FAILED,
            actor_user_id=user.id if user else None,
            actor_email=normalized_email,
            owner_user_id=user.id if user else None,
            severity="warning",
            ip_address=ip_address,
            user_agent=user_agent,
            commit=True,
        )
        raise AuthError(INVALID_CREDENTIALS_MESSAGE)

    if user.locked_until and user.locked_until > utcnow():
        await record_audit_event(
            session,
            action=AuditAction.LOGIN_BLOCKED,
            actor_user_id=user.id,
            actor_email=user.email,
            owner_user_id=user.id,
            severity="warning",
            ip_address=ip_address,
            user_agent=user_agent,
            details={"locked_until": user.locked_until.isoformat()},
            commit=True,
        )
        raise AuthError("Учетная запись временно заблокирована. Повторите позже.")

    if not user.is_active:
        await record_audit_event(
            session,
            action=AuditAction.LOGIN_BLOCKED,
            actor_user_id=user.id,
            actor_email=user.email,
            owner_user_id=user.id,
            severity="warning",
            ip_address=ip_address,
            user_agent=user_agent,
            details={"reason": "inactive"},
            commit=True,
        )
        raise ForbiddenError("Учетная запись отключена. Обратитесь к администратору.")

    # Opportunistically upgrade the hash if Argon2 parameters changed.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    user.last_activity_at = utcnow()

    if user.profile is None:
        session.add(UserProfile(user_id=user.id))

    return user


async def create_session(
    db: AsyncSession,
    user: User,
    *,
    ip_address: Optional[str],
    user_agent: Optional[str],
) -> tuple[UserSession, str]:
    """Create a session and return it with the plaintext refresh token.

    The plaintext value is returned once, for the Set-Cookie header only.
    """
    settings = get_settings()
    refresh_token = generate_refresh_token()

    user_session = UserSession(
        owner_user_id=user.id,
        refresh_token_hash=hash_refresh_token(refresh_token),
        user_agent=(user_agent or "")[:512] or None,
        ip_address=ip_address,
        expires_at=refresh_token_expiry(),
        last_used_at=utcnow(),
    )
    db.add(user_session)
    await db.flush()

    await _enforce_session_limit(db, user.id, settings.max_active_sessions)
    return user_session, refresh_token


async def _enforce_session_limit(db: AsyncSession, user_id: uuid.UUID, limit: int) -> None:
    """Revoke the oldest sessions beyond the configured maximum."""
    result = await db.execute(
        select(UserSession.id)
        .where(
            UserSession.owner_user_id == user_id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > utcnow(),
        )
        .order_by(UserSession.last_used_at.desc().nullslast(), UserSession.created_at.desc())
        .offset(limit)
    )
    stale_ids = list(result.scalars().all())
    if stale_ids:
        await db.execute(
            update(UserSession)
            .where(UserSession.id.in_(stale_ids))
            .values(revoked_at=utcnow())
        )


async def rotate_session(
    db: AsyncSession,
    refresh_token: str,
    *,
    ip_address: Optional[str],
    user_agent: Optional[str],
) -> tuple[User, UserSession, str]:
    """Validate a refresh token and issue a fresh one (single-use rotation)."""
    token_hash = hash_refresh_token(refresh_token or "")
    if not refresh_token:
        raise AuthError("Отсутствует refresh-токен")

    result = await db.execute(
        select(UserSession).where(UserSession.refresh_token_hash == token_hash)
    )
    user_session = result.scalar_one_or_none()

    if user_session is None:
        await record_audit_event(
            db,
            action=AuditAction.TOKEN_REFRESH_REJECTED,
            severity="warning",
            ip_address=ip_address,
            user_agent=user_agent,
            details={"reason": "unknown_token"},
            commit=True,
        )
        raise AuthError("Недействительный refresh-токен")

    now = utcnow()
    if user_session.revoked_at is not None or user_session.expires_at <= now:
        # A revoked token being replayed means the cookie may be compromised:
        # drop every session for that user.
        await db.execute(
            update(UserSession)
            .where(
                UserSession.owner_user_id == user_session.owner_user_id,
                UserSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await record_audit_event(
            db,
            action=AuditAction.TOKEN_REFRESH_REJECTED,
            actor_user_id=user_session.owner_user_id,
            owner_user_id=user_session.owner_user_id,
            severity="critical" if user_session.revoked_at else "warning",
            ip_address=ip_address,
            user_agent=user_agent,
            details={"reason": "revoked_reuse" if user_session.revoked_at else "expired"},
            commit=True,
        )
        raise AuthError("Сессия истекла, войдите заново")

    user = await db.get(User, user_session.owner_user_id)
    if user is None or not user.is_active:
        user_session.revoked_at = now
        await db.commit()
        raise ForbiddenError("Учетная запись отключена")

    new_refresh_token = generate_refresh_token()
    user_session.refresh_token_hash = hash_refresh_token(new_refresh_token)
    user_session.expires_at = refresh_token_expiry()
    user_session.last_used_at = now
    user_session.ip_address = ip_address or user_session.ip_address
    user_session.user_agent = (user_agent or user_session.user_agent or "")[:512] or None
    user.last_activity_at = now

    return user, user_session, new_refresh_token


async def revoke_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    await db.execute(
        update(UserSession)
        .where(UserSession.id == session_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


async def revoke_session_by_refresh_token(
    db: AsyncSession, refresh_token: str
) -> Optional[UserSession]:
    """Revoke the session matching a refresh token (logout).

    Returns the session so the caller can audit it. Idempotent: an unknown or
    already-revoked token is a no-op that returns None.
    """
    token_hash = hash_refresh_token(refresh_token or "")
    result = await db.execute(
        select(UserSession).where(UserSession.refresh_token_hash == token_hash)
    )
    user_session = result.scalar_one_or_none()
    if user_session is None:
        return None
    if user_session.revoked_at is None:
        user_session.revoked_at = utcnow()
    return user_session


async def revoke_all_sessions(
    db: AsyncSession, user_id: uuid.UUID, *, except_session_id: Optional[uuid.UUID] = None
) -> int:
    """Revoke every active session for a user (used on password change)."""
    stmt = (
        update(UserSession)
        .where(UserSession.owner_user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    if except_session_id is not None:
        stmt = stmt.where(UserSession.id != except_session_id)
    result = await db.execute(stmt)
    return int(result.rowcount or 0)


async def list_active_sessions(db: AsyncSession, user_id: uuid.UUID) -> list[UserSession]:
    result = await db.execute(
        select(UserSession)
        .where(
            UserSession.owner_user_id == user_id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > utcnow(),
        )
        .order_by(UserSession.last_used_at.desc().nullslast())
    )
    return list(result.scalars().all())
