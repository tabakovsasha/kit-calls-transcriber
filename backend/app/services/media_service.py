"""Signed, short-lived grants for resources that cannot carry an Authorization header.

``<audio src>`` and ``new WebSocket()`` cannot send a bearer token, so instead
of exposing the upstream record URL (which embeds an access token) we hand the
browser an HMAC-signed, owner-scoped, expiring URL that points back at us.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from urllib.parse import urlencode

from app.core.config import get_settings
from app.core.errors import ForbiddenError
from app.core.security import sign_payload, verify_signed_payload

AUDIO_SCOPE = "audio"
WS_SCOPE = "ws"

AUDIO_DIR = Path("audio-cache")


def ensure_audio_dir() -> Path:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    return AUDIO_DIR


def audio_filename(connection_id: uuid.UUID | str, call_id: str) -> str:
    """Deterministic, path-safe cache filename.

    The call id comes from upstream, so it is hashed rather than interpolated
    into a path: that removes any traversal risk.
    """
    digest = hashlib.sha256(f"{connection_id}:{call_id}".encode("utf-8")).hexdigest()
    return f"{digest}.mp3"


def audio_path(connection_id: uuid.UUID | str, call_id: str) -> Path:
    return ensure_audio_dir() / audio_filename(connection_id, call_id)


def build_audio_url(
    owner_user_id: uuid.UUID | str,
    connection_id: uuid.UUID | str,
    call_id: str,
) -> tuple[str, int]:
    """Return (relative_url, expires_at_epoch) for browser playback."""
    settings = get_settings()
    subject = f"{connection_id}:{call_id}"
    signature, expires_at = sign_payload(
        AUDIO_SCOPE, subject, str(owner_user_id), settings.media_url_ttl_seconds
    )
    query = urlencode(
        {
            "connection_id": str(connection_id),
            "call_id": call_id,
            "owner": str(owner_user_id),
            "expires": expires_at,
            "sig": signature,
        }
    )
    return f"/api/audio/stream?{query}", expires_at


def assert_audio_grant(
    *,
    connection_id: str,
    call_id: str,
    owner: str,
    expires: int,
    signature: str,
) -> None:
    """Validate a playback grant. Raises ForbiddenError when it does not hold."""
    subject = f"{connection_id}:{call_id}"
    if not verify_signed_payload(AUDIO_SCOPE, subject, owner, expires, signature):
        raise ForbiddenError("Ссылка на запись недействительна или истекла")


def build_ws_ticket(owner_user_id: uuid.UUID | str, session_id: uuid.UUID | str) -> tuple[str, int]:
    """Issue a one-minute websocket ticket bound to the caller's session."""
    settings = get_settings()
    signature, expires_at = sign_payload(
        WS_SCOPE, str(session_id), str(owner_user_id), settings.ws_ticket_ttl_seconds
    )
    ticket = f"{owner_user_id}.{session_id}.{expires_at}.{signature}"
    return ticket, expires_at


def parse_ws_ticket(ticket: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Validate a websocket ticket and return (owner_user_id, session_id)."""
    try:
        owner, session_id, expires_raw, signature = ticket.split(".")
        expires_at = int(expires_raw)
    except (AttributeError, ValueError) as exc:
        raise ForbiddenError("Некорректный тикет подключения") from exc

    if not verify_signed_payload(WS_SCOPE, session_id, owner, expires_at, signature):
        raise ForbiddenError("Тикет подключения недействителен или истек")

    try:
        return uuid.UUID(owner), uuid.UUID(session_id)
    except ValueError as exc:
        raise ForbiddenError("Некорректный тикет подключения") from exc
