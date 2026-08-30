"""Voximplant connection service.

Ownership and secrecy rules enforced here:
- every read is scoped by owner_user_id, so one user can never touch another
  user's connection (admins act through an explicit owner override);
- access tokens are encrypted with AES-256-GCM before insert and decrypted
  only in-memory, for the duration of a single upstream operation;
- responses expose a mask plus a short fingerprint, never the token.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import (
    DecryptionError,
    decrypt_secret,
    encrypt_secret,
    mask_secret,
    secret_fingerprint,
)
from app.core.errors import ConflictError, NotFoundError, UpstreamError, ValidationError
from app.core.net_guard import normalize_host
from app.core.security import utcnow
from app.db.models import ConnectionStatus, VoximplantConnection
from app.schemas.connections import ConnectionResponse
from app.services.voximplant_client import UpstreamCredentials, VoximplantClient

logger = logging.getLogger(__name__)


def to_response(
    connection: VoximplantConnection, *, owner_email: Optional[str] = None
) -> ConnectionResponse:
    """Serialize a connection without ever touching the plaintext token."""
    return ConnectionResponse(
        id=connection.id,
        label=connection.label,
        api_host=connection.api_host,
        domain=connection.domain,
        is_active=connection.is_active,
        is_default=connection.is_default,
        last_connection_status=connection.last_connection_status,
        last_connection_error=connection.last_connection_error,
        last_checked_at=connection.last_checked_at,
        last_sync_at=connection.last_sync_at,
        account_name=connection.account_name,
        account_id=connection.account_id,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
        access_token_rotated_at=connection.access_token_rotated_at,
        access_token_masked=mask_secret(),
        access_token_fingerprint=connection.access_token_fingerprint,
        owner_user_id=connection.owner_user_id,
        owner_email=owner_email,
    )


async def list_connections(
    db: AsyncSession, owner_user_id: uuid.UUID
) -> Sequence[VoximplantConnection]:
    result = await db.execute(
        select(VoximplantConnection)
        .where(
            VoximplantConnection.owner_user_id == owner_user_id,
            VoximplantConnection.deleted_at.is_(None),
        )
        .order_by(
            VoximplantConnection.is_default.desc(), VoximplantConnection.created_at.asc()
        )
    )
    return result.scalars().all()


async def get_owned_connection(
    db: AsyncSession, connection_id: uuid.UUID, owner_user_id: uuid.UUID
) -> VoximplantConnection:
    """Fetch a connection or raise 404. Never leaks existence across owners."""
    result = await db.execute(
        select(VoximplantConnection).where(
            VoximplantConnection.id == connection_id,
            VoximplantConnection.owner_user_id == owner_user_id,
            VoximplantConnection.deleted_at.is_(None),
        )
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise NotFoundError("Подключение не найдено")
    return connection


async def resolve_credentials(
    db: AsyncSession, connection_id: uuid.UUID, owner_user_id: uuid.UUID
) -> tuple[VoximplantConnection, UpstreamCredentials]:
    """Decrypt a token just-in-time for one upstream operation."""
    connection = await get_owned_connection(db, connection_id, owner_user_id)
    if not connection.is_active:
        raise ValidationError("Подключение отключено")

    try:
        access_token = decrypt_secret(
            connection.access_token_ciphertext,
            connection.access_token_iv,
            connection.access_token_auth_tag,
        )
    except DecryptionError as exc:
        connection.last_connection_status = ConnectionStatus.ERROR
        connection.last_connection_error = "Не удалось расшифровать сохраненный токен"
        await db.commit()
        raise ValidationError(
            "Сохраненный токен недействителен, обновите его в настройках подключения"
        ) from exc

    return connection, UpstreamCredentials(
        api_host=connection.api_host,
        domain=connection.domain,
        access_token=access_token,
    )


async def _clear_other_defaults(
    db: AsyncSession, owner_user_id: uuid.UUID, keep_id: Optional[uuid.UUID]
) -> None:
    stmt = (
        update(VoximplantConnection)
        .where(
            VoximplantConnection.owner_user_id == owner_user_id,
            VoximplantConnection.is_default.is_(True),
        )
        .values(is_default=False)
    )
    if keep_id is not None:
        stmt = stmt.where(VoximplantConnection.id != keep_id)
    await db.execute(stmt)


async def _assert_unique(
    db: AsyncSession,
    owner_user_id: uuid.UUID,
    api_host: str,
    domain: str,
    *,
    exclude_id: Optional[uuid.UUID] = None,
) -> None:
    stmt = (
        select(func.count())
        .select_from(VoximplantConnection)
        .where(
            VoximplantConnection.owner_user_id == owner_user_id,
            VoximplantConnection.api_host == api_host,
            VoximplantConnection.domain == domain,
            VoximplantConnection.deleted_at.is_(None),
        )
    )
    if exclude_id is not None:
        stmt = stmt.where(VoximplantConnection.id != exclude_id)
    if int((await db.execute(stmt)).scalar() or 0) > 0:
        raise ConflictError("Подключение с таким хостом и доменом уже существует")


async def _verify_credentials(
    credentials: UpstreamCredentials,
) -> tuple[ConnectionStatus, Optional[str]]:
    """Probe the upstream API. Returns a status plus a safe error message."""
    try:
        await VoximplantClient(credentials).verify()
        return ConnectionStatus.CONNECTED, None
    except UpstreamError as exc:
        status = (
            ConnectionStatus.INVALID_CREDENTIALS
            if exc.code in {"UPSTREAM_AUTH_FAILED", "UPSTREAM_FORBIDDEN"}
            else ConnectionStatus.DISCONNECTED
        )
        return status, exc.message


async def create_connection(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    label: str,
    api_host: str,
    domain: str,
    access_token: str,
    is_default: bool,
    verify: bool,
) -> VoximplantConnection:
    normalized_host = normalize_host(api_host)
    await _assert_unique(db, owner_user_id, normalized_host, domain)

    status = ConnectionStatus.UNKNOWN
    error_message: Optional[str] = None
    if verify:
        status, error_message = await _verify_credentials(
            UpstreamCredentials(
                api_host=normalized_host, domain=domain, access_token=access_token
            )
        )

    encrypted = encrypt_secret(access_token)
    existing_count = len(await list_connections(db, owner_user_id))
    make_default = is_default or existing_count == 0

    connection = VoximplantConnection(
        owner_user_id=owner_user_id,
        label=label,
        api_host=normalized_host,
        domain=domain,
        access_token_ciphertext=encrypted.ciphertext,
        access_token_iv=encrypted.iv,
        access_token_auth_tag=encrypted.auth_tag,
        access_token_key_version=encrypted.key_version,
        access_token_fingerprint=secret_fingerprint(access_token),
        access_token_rotated_at=utcnow(),
        is_default=make_default,
        last_connection_status=status,
        last_connection_error=error_message,
        last_checked_at=utcnow() if verify else None,
    )
    db.add(connection)
    await db.flush()

    if make_default:
        await _clear_other_defaults(db, owner_user_id, connection.id)

    return connection


async def update_connection(
    db: AsyncSession,
    connection: VoximplantConnection,
    *,
    label: Optional[str] = None,
    api_host: Optional[str] = None,
    domain: Optional[str] = None,
    is_active: Optional[bool] = None,
    is_default: Optional[bool] = None,
) -> VoximplantConnection:
    if api_host is not None or domain is not None:
        new_host = normalize_host(api_host) if api_host is not None else connection.api_host
        new_domain = domain if domain is not None else connection.domain
        await _assert_unique(
            db, connection.owner_user_id, new_host, new_domain, exclude_id=connection.id
        )
        connection.api_host = new_host
        connection.domain = new_domain
        # Endpoint changed: the previous verification result no longer applies.
        connection.last_connection_status = ConnectionStatus.UNKNOWN
        connection.last_connection_error = None
        connection.last_checked_at = None

    if label is not None:
        connection.label = label
    if is_active is not None:
        connection.is_active = is_active

    if is_default:
        connection.is_default = True
        await _clear_other_defaults(db, connection.owner_user_id, connection.id)
    elif is_default is False:
        connection.is_default = False

    return connection


async def rotate_token(
    db: AsyncSession,
    connection: VoximplantConnection,
    *,
    access_token: str,
    verify: bool,
) -> VoximplantConnection:
    """Replace the stored token with a freshly encrypted one."""
    if verify:
        status, error_message = await _verify_credentials(
            UpstreamCredentials(
                api_host=connection.api_host,
                domain=connection.domain,
                access_token=access_token,
            )
        )
        connection.last_connection_status = status
        connection.last_connection_error = error_message
        connection.last_checked_at = utcnow()

    encrypted = encrypt_secret(access_token)
    connection.access_token_ciphertext = encrypted.ciphertext
    connection.access_token_iv = encrypted.iv
    connection.access_token_auth_tag = encrypted.auth_tag
    connection.access_token_key_version = encrypted.key_version
    connection.access_token_fingerprint = secret_fingerprint(access_token)
    connection.access_token_rotated_at = utcnow()
    return connection


async def verify_connection(
    db: AsyncSession, connection: VoximplantConnection, credentials: UpstreamCredentials
) -> VoximplantConnection:
    status, error_message = await _verify_credentials(credentials)
    connection.last_connection_status = status
    connection.last_connection_error = error_message
    connection.last_checked_at = utcnow()
    if status == ConnectionStatus.CONNECTED:
        connection.last_sync_at = utcnow()
    return connection


async def soft_delete_connection(db: AsyncSession, connection: VoximplantConnection) -> None:
    """Soft-delete and scrub the ciphertext so the token becomes unrecoverable."""
    connection.deleted_at = utcnow()
    connection.is_active = False
    connection.is_default = False
    connection.access_token_ciphertext = ""
    connection.access_token_iv = ""
    connection.access_token_auth_tag = ""
    connection.access_token_fingerprint = None

    # Promote another connection so the user keeps a working default.
    for candidate in await list_connections(db, connection.owner_user_id):
        if candidate.id != connection.id and candidate.is_active:
            candidate.is_default = True
            break


async def get_default_connection(
    db: AsyncSession, owner_user_id: uuid.UUID
) -> Optional[VoximplantConnection]:
    connections = await list_connections(db, owner_user_id)
    for connection in connections:
        if connection.is_default and connection.is_active:
            return connection
    return next((item for item in connections if item.is_active), None)


async def count_connections(db: AsyncSession, owner_user_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(VoximplantConnection)
        .where(
            VoximplantConnection.owner_user_id == owner_user_id,
            VoximplantConnection.deleted_at.is_(None),
        )
    )
    return int(result.scalar() or 0)
