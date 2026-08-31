"""Realtime websocket endpoint for queue and transcription events.

A browser cannot attach an ``Authorization`` header to ``new WebSocket()``, so
the handshake is authorised by a short-lived, HMAC-signed ticket obtained from
``POST /api/auth/ws-ticket`` while holding a valid access token.

The ticket is bound to the issuing *session*, not just the user: logging out or
changing the password revokes the session and any ticket minted from it stops
working, even before it expires.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from app.core.errors import AppError
from app.core.security import utcnow
from app.db.models import User, UserSession
from app.db.session import SessionFactory
from app.services import media_service
from app.services.events_service import hub

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ws"])


async def _resolve_ticket(ticket: Optional[str]):
    """Return the owner id for a valid ticket, or None when it is not usable.

    Every rejection path returns None: the client is never told which check
    failed (bad signature, expired, revoked session, disabled account).
    """
    if not ticket:
        return None

    try:
        owner_user_id, session_id = media_service.parse_ws_ticket(ticket)
    except AppError:
        return None

    # A signature alone is not enough: the session behind it must still be live.
    async with SessionFactory() as db:
        result = await db.execute(
            select(UserSession).where(
                UserSession.id == session_id,
                UserSession.owner_user_id == owner_user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > utcnow(),
            )
        )
        if result.scalar_one_or_none() is None:
            return None

        user = await db.get(User, owner_user_id)
        if user is None or not user.is_active:
            return None

    return owner_user_id


@router.websocket("/ws")
async def events_socket(websocket: WebSocket, ticket: Optional[str] = None) -> None:
    """Stream owner-scoped events until the client goes away.

    The socket is registered under the ticket's owner id, so ``hub.publish``
    can only ever reach the user the event belongs to.
    """
    owner_user_id = await _resolve_ticket(ticket)
    if owner_user_id is None:
        # Rejected before accept: the handshake fails with HTTP 403 and no
        # application data is ever exchanged.
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    await hub.register(owner_user_id, websocket)
    logger.info(
        "[WS] Клиент подключен owner=%s sockets=%s",
        owner_user_id,
        hub.owner_count(owner_user_id),
    )

    try:
        await websocket.send_json({"type": "ready"})
        while True:
            # The client is not required to say anything; this read exists to
            # observe the disconnect and to answer keepalives.
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.info("[WS] Соединение прервано owner=%s: %s", owner_user_id, exc)
    finally:
        # Runs on every exit path, so a closed browser tab cannot leak a socket.
        await hub.unregister(owner_user_id, websocket)
        logger.info(
            "[WS] Клиент отключен owner=%s sockets=%s",
            owner_user_id,
            hub.owner_count(owner_user_id),
        )
