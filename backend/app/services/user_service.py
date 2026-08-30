"""User and profile service (admin management + self-service).

Ownership: profile updates always target the authenticated user unless the
caller is an admin acting on an explicit target id (checked at the route).
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.security import (
    hash_password,
    utcnow,
    validate_password_strength,
    verify_password,
)
from app.db.models import User, UserProfile, UserRole, VoximplantConnection

logger = logging.getLogger(__name__)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


async def _assert_email_free(
    db: AsyncSession, email: str, *, exclude_id: Optional[uuid.UUID] = None
) -> None:
    stmt = select(func.count()).select_from(User).where(func.lower(User.email) == email)
    if exclude_id is not None:
        stmt = stmt.where(User.id != exclude_id)
    if int((await db.execute(stmt)).scalar() or 0) > 0:
        raise ConflictError("Пользователь с таким email уже существует")


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise NotFoundError("Пользователь не найден")
    return user


async def list_users(
    db: AsyncSession,
    *,
    search: Optional[str] = None,
    role: Optional[UserRole] = None,
    is_active: Optional[bool] = None,
    page: int = 1,
    limit: int = 25,
) -> tuple[Sequence[User], int, dict[uuid.UUID, int]]:
    """List users with pagination plus a connection count per user."""
    conditions = []
    if search:
        pattern = f"%{search.strip().lower()}%"
        conditions.append(
            or_(
                func.lower(User.email).like(pattern),
                User.id.in_(
                    select(UserProfile.user_id).where(
                        or_(
                            func.lower(UserProfile.first_name).like(pattern),
                            func.lower(UserProfile.last_name).like(pattern),
                            func.lower(UserProfile.display_name).like(pattern),
                        )
                    )
                ),
            )
        )
    if role is not None:
        conditions.append(User.role == role)
    if is_active is not None:
        conditions.append(User.is_active == is_active)

    count_stmt = select(func.count()).select_from(User)
    items_stmt = select(User).order_by(User.created_at.asc())
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
        items_stmt = items_stmt.where(condition)

    total = int((await db.execute(count_stmt)).scalar() or 0)
    page = max(1, page)
    limit = max(1, min(100, limit))
    users = list(
        (await db.execute(items_stmt.offset((page - 1) * limit).limit(limit))).scalars().all()
    )

    counts: dict[uuid.UUID, int] = {}
    if users:
        rows = await db.execute(
            select(VoximplantConnection.owner_user_id, func.count())
            .where(
                VoximplantConnection.owner_user_id.in_([item.id for item in users]),
                VoximplantConnection.deleted_at.is_(None),
            )
            .group_by(VoximplantConnection.owner_user_id)
        )
        counts = {row[0]: int(row[1]) for row in rows.all()}

    return users, total, counts


async def create_user(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    role: UserRole,
    is_active: bool,
    must_change_password: bool,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
) -> User:
    normalized = normalize_email(email)
    await _assert_email_free(db, normalized)

    weakness = validate_password_strength(password)
    if weakness:
        raise ValidationError(weakness)

    user = User(
        email=normalized,
        password_hash=hash_password(password),
        role=role,
        is_active=is_active,
        must_change_password=must_change_password,
        password_changed_at=utcnow(),
    )
    db.add(user)
    await db.flush()

    display_name = " ".join(part for part in [first_name, last_name] if part).strip() or None
    db.add(
        UserProfile(
            user_id=user.id,
            first_name=first_name,
            last_name=last_name,
            display_name=display_name,
        )
    )
    await db.flush()
    return user


async def update_user(
    db: AsyncSession,
    user: User,
    *,
    actor: User,
    email: Optional[str] = None,
    role: Optional[UserRole] = None,
    is_active: Optional[bool] = None,
    must_change_password: Optional[bool] = None,
) -> tuple[User, dict[str, object]]:
    """Apply an admin update. Returns the user plus a diff for the audit log."""
    changes: dict[str, object] = {}

    if email is not None:
        normalized = normalize_email(email)
        if normalized != user.email:
            await _assert_email_free(db, normalized, exclude_id=user.id)
            changes["email"] = {"from": user.email, "to": normalized}
            user.email = normalized

    if role is not None and role != user.role:
        # Guard against removing the last administrator or self-demotion.
        if user.role == UserRole.ADMIN and role != UserRole.ADMIN:
            if actor.id == user.id:
                raise ForbiddenError("Нельзя понизить собственную роль администратора")
            if await count_active_admins(db, exclude_id=user.id) == 0:
                raise ValidationError("В системе должен остаться хотя бы один администратор")
        changes["role"] = {"from": user.role.value, "to": role.value}
        user.role = role

    if is_active is not None and is_active != user.is_active:
        if not is_active:
            if actor.id == user.id:
                raise ForbiddenError("Нельзя отключить собственную учетную запись")
            if user.role == UserRole.ADMIN and await count_active_admins(db, exclude_id=user.id) == 0:
                raise ValidationError("В системе должен остаться хотя бы один администратор")
        changes["is_active"] = {"from": user.is_active, "to": is_active}
        user.is_active = is_active

    if must_change_password is not None and must_change_password != user.must_change_password:
        changes["must_change_password"] = must_change_password
        user.must_change_password = must_change_password

    return user, changes


async def count_active_admins(
    db: AsyncSession, *, exclude_id: Optional[uuid.UUID] = None
) -> int:
    stmt = (
        select(func.count())
        .select_from(User)
        .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
    )
    if exclude_id is not None:
        stmt = stmt.where(User.id != exclude_id)
    return int((await db.execute(stmt)).scalar() or 0)


async def change_own_password(
    db: AsyncSession, user: User, *, current_password: str, new_password: str
) -> User:
    """Self-service password change; requires the current password."""
    if not verify_password(user.password_hash, current_password):
        raise ValidationError("Текущий пароль указан неверно")
    if verify_password(user.password_hash, new_password):
        raise ValidationError("Новый пароль должен отличаться от текущего")

    weakness = validate_password_strength(new_password)
    if weakness:
        raise ValidationError(weakness)

    user.password_hash = hash_password(new_password)
    user.password_changed_at = utcnow()
    user.must_change_password = False
    return user


async def admin_reset_password(
    db: AsyncSession, user: User, *, new_password: str, must_change_password: bool
) -> User:
    weakness = validate_password_strength(new_password)
    if weakness:
        raise ValidationError(weakness)

    user.password_hash = hash_password(new_password)
    user.password_changed_at = utcnow()
    user.must_change_password = must_change_password
    return user


async def upsert_profile(
    db: AsyncSession,
    user: User,
    *,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    display_name: Optional[str] = None,
    timezone: Optional[str] = None,
    language: Optional[str] = None,
    avatar_url: Optional[str] = None,
) -> UserProfile:
    profile = user.profile
    if profile is None:
        profile = UserProfile(user_id=user.id)
        db.add(profile)
        await db.flush()

    for field, value in (
        ("first_name", first_name),
        ("last_name", last_name),
        ("display_name", display_name),
        ("timezone", timezone),
        ("language", language),
        ("avatar_url", avatar_url),
    ):
        if value is not None:
            setattr(profile, field, value.strip() or None if isinstance(value, str) else value)

    if not profile.display_name:
        combined = " ".join(
            part for part in [profile.first_name, profile.last_name] if part
        ).strip()
        profile.display_name = combined or None

    # timezone/language are non-nullable columns: keep their defaults.
    profile.timezone = profile.timezone or "UTC"
    profile.language = profile.language or "ru"
    return profile
