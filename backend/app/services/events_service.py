"""Per-owner websocket hub for live queue and transcription events.

The legacy app kept one global broadcast list, so every browser saw every
session's progress. Here sockets are bucketed by owner_user_id, and a payload is
only ever delivered to the owner it belongs to.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionHub:
    """Tracks live sockets grouped by owner."""

    def __init__(self) -> None:
        self._sockets: dict[uuid.UUID, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def register(self, owner_user_id: uuid.UUID, socket: WebSocket) -> None:
        async with self._lock:
            self._sockets.setdefault(owner_user_id, set()).add(socket)

    async def unregister(self, owner_user_id: uuid.UUID, socket: WebSocket) -> None:
        async with self._lock:
            sockets = self._sockets.get(owner_user_id)
            if not sockets:
                return
            sockets.discard(socket)
            if not sockets:
                self._sockets.pop(owner_user_id, None)

    async def publish(self, owner_user_id: uuid.UUID, payload: dict[str, Any]) -> None:
        """Send a payload to every live socket of one owner.

        Dead sockets are dropped silently: a disconnect must never surface as a
        failure in the transcription pipeline.
        """
        async with self._lock:
            targets = list(self._sockets.get(owner_user_id, ()))

        if not targets:
            return

        stale: list[WebSocket] = []
        for socket in targets:
            try:
                await socket.send_json(payload)
            except Exception:
                stale.append(socket)

        for socket in stale:
            await self.unregister(owner_user_id, socket)

    def owner_count(self, owner_user_id: uuid.UUID) -> int:
        return len(self._sockets.get(owner_user_id, ()))


hub = ConnectionHub()
