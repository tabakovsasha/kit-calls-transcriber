"""Audit logging service.

Every security-relevant action is written to audit_events. Payloads pass
through redact_mapping() so tokens and passwords can never be persisted.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import redact_mapping
from app.db.models import AuditEvent

logger = logging.getLogger(__name__)


class AuditAction:
    LOGIN_SUCCESS = "auth.login.success"
    LOGIN_FAILED = "auth.login.failed"
    LOGIN_BLOCKED = "auth.login.blocked"
    LOGOUT = "auth.logout"
    TOKEN_REFRESH = "auth.token.refresh"
    TOKEN_REFRESH_REJECTED = "auth.token.refresh_rejected"
    UNVERIFIED_TOKEN_ATTEMPT = "auth.token.unverified_attempt"
    SESSIONS_REVOKED = "auth.sessions.revoked"

    USER_CREATED = "user.created"
    USER_UPDATED = "user.updated"
    USER_ROLE_CHANGED = "user.role.changed"
    USER_ACTIVATED = "user.activated"
    USER_DEACTIVATED = "user.deactivated"
    PASSWORD_CHANGED = "user.password.changed"
    PASSWORD_RESET_BY_ADMIN = "user.password.reset_by_admin"
    PROFILE_UPDATED = "profile.updated"

    CONNECTION_CREATED = "connection.created"
    CONNECTION_UPDATED = "connection.updated"
    CONNECTION_DELETED = "connection.deleted"
    CONNECTION_DEFAULT_CHANGED = "connection.default.changed"
    CONNECTION_TOKEN_ROTATED = "connection.token.rotated"
    CONNECTION_VERIFIED = "connection.verified"
    CONNECTION_VERIFY_FAILED = "connection.verify_failed"

    QUEUE_ITEM_ADDED = "queue.item.added"
    QUEUE_CLEARED = "queue.cleared"
    SCHEDULE_CREATED = "schedule.created"
    SCHEDULE_UPDATED = "schedule.updated"
    SCHEDULE_DELETED = "schedule.deleted"

    ACCESS_DENIED = "security.access_denied"


async def record_audit_event(
    session: AsyncSession,
    *,
    action: str,
    actor_user_id: Optional[uuid.UUID] = None,
    actor_email: Optional[str] = None,
    owner_user_id: Optional[uuid.UUID] = None,
    severity: str = "info",
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    target_label: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
    commit: bool = False,
) -> None:
    """Append an audit event. Never raises into the caller's request path."""
    try:
        event = AuditEvent(
            action=action,
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            owner_user_id=owner_user_id,
            severity=severity,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            target_label=target_label,
            ip_address=ip_address,
            user_agent=(user_agent or "")[:512] or None,
            details=redact_mapping(details) if details else None,
        )
        session.add(event)
        if commit:
            await session.commit()
    except Exception as exc:
        logger.error("[AUDIT] Не удалось записать событие %s: %s", action, exc)


async def list_audit_events(
    session: AsyncSession,
    *,
    owner_user_id: Optional[uuid.UUID] = None,
    action: Optional[str] = None,
    page: int = 1,
    limit: int = 50,
) -> tuple[list[AuditEvent], int]:
    """List audit events. owner_user_id=None means system-wide (admin only)."""
    from sqlalchemy import func

    conditions = []
    if owner_user_id is not None:
        conditions.append(AuditEvent.owner_user_id == owner_user_id)
    if action:
        conditions.append(AuditEvent.action == action)

    count_stmt = select(func.count()).select_from(AuditEvent)
    items_stmt = select(AuditEvent).order_by(AuditEvent.created_at.desc())
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
        items_stmt = items_stmt.where(condition)

    total = int((await session.execute(count_stmt)).scalar() or 0)
    page = max(1, page)
    limit = max(1, min(200, limit))
    items = (
        (await session.execute(items_stmt.offset((page - 1) * limit).limit(limit)))
        .scalars()
        .all()
    )
    return list(items), total
