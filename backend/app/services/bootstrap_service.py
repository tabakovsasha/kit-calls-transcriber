"""First-run bootstrap of the initial administrator.

Runs once per startup and is deliberately conservative:
- it never touches an existing installation (any active admin means skip);
- it never invents a password, the operator must supply INIT_ADMIN_PASSWORD
  (``scripts/init-secrets.sh`` generates one);
- it reuses ``user_service.create_user``, so password policy, email
  normalization and profile creation behave exactly like the admin API;
- the password is never logged, only the email is.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import UserRole
from app.services import user_service
from app.services.audit_service import AuditAction, record_audit_event

logger = logging.getLogger(__name__)


async def ensure_bootstrap_admin(db: AsyncSession, settings: Settings) -> None:
    """Create the initial admin when the installation has none."""
    existing_admins = await user_service.count_active_admins(db)
    if existing_admins > 0:
        logger.info("[BOOTSTRAP] Активных администраторов: %s, создание не требуется", existing_admins)
        return

    email = user_service.normalize_email(settings.init_admin_email)
    if not email or not settings.init_admin_password:
        logger.warning(
            "[BOOTSTRAP] В системе нет активного администратора, но INIT_ADMIN_EMAIL/"
            "INIT_ADMIN_PASSWORD не заданы. Задайте их в .env (см. scripts/init-secrets.sh) "
            "и перезапустите приложение."
        )
        return

    try:
        user = await user_service.create_user(
            db,
            email=email,
            password=settings.init_admin_password,
            role=UserRole.ADMIN,
            is_active=True,
            must_change_password=settings.init_admin_must_change_password,
        )
        await record_audit_event(
            db,
            action=AuditAction.USER_CREATED,
            actor_email="system:bootstrap",
            owner_user_id=user.id,
            severity="warning",
            target_type="user",
            target_id=str(user.id),
            target_label=user.email,
            details={
                "role": user.role.value,
                "must_change_password": user.must_change_password,
                "source": "bootstrap",
            },
        )
        await db.commit()
    except AppError as exc:
        # Weak password, or the email is taken by a disabled/non-admin account.
        await db.rollback()
        logger.error(
            "[BOOTSTRAP] Не удалось создать администратора %s: %s. "
            "Исправьте INIT_ADMIN_* в .env и перезапустите приложение.",
            email,
            exc.message,
        )
        return
    except IntegrityError:
        # Another worker process won the race; that instance did the work.
        await db.rollback()
        logger.info("[BOOTSTRAP] Администратор %s уже создан другим процессом", email)
        return

    logger.warning(
        "[BOOTSTRAP] Создан первоначальный администратор %s (must_change_password=%s). "
        "Смените пароль после первого входа.",
        user.email,
        user.must_change_password,
    )
