"""Short-lived, in-memory cache of upstream call metadata.

Why this exists: the queue needs ``record_url`` to download audio, but that URL
embeds an access token, so it must never be handed to a browser and cannot be
sent back by the client. Search results are therefore cached server-side, keyed
by owner + connection + call, and the client only ever sends call ids.

Bounded and TTL-based on purpose: a miss is recoverable (the user re-runs the
search) and nothing sensitive should outlive its usefulness.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

CACHE_TTL_SECONDS = 3600
MAX_ENTRIES = 20000

_entries: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}


def _key(owner_user_id: uuid.UUID | str, connection_id: uuid.UUID | str, call_id: str):
    return (str(owner_user_id), str(connection_id), call_id)


def _evict_expired(now: float) -> None:
    expired = [key for key, (stored_at, _) in _entries.items() if now - stored_at > CACHE_TTL_SECONDS]
    for key in expired:
        _entries.pop(key, None)


def remember(
    owner_user_id: uuid.UUID | str,
    connection_id: uuid.UUID | str,
    call_id: str,
    metadata: dict[str, Any],
) -> None:
    """Store metadata for one call. Overwrites any previous entry."""
    now = time.monotonic()
    _evict_expired(now)

    if len(_entries) >= MAX_ENTRIES:
        # Drop the oldest entries rather than growing without bound.
        oldest = sorted(_entries.items(), key=lambda pair: pair[1][0])[: MAX_ENTRIES // 4]
        for key, _ in oldest:
            _entries.pop(key, None)

    _entries[_key(owner_user_id, connection_id, call_id)] = (now, dict(metadata))


def remember_many(
    owner_user_id: uuid.UUID | str,
    connection_id: uuid.UUID | str,
    items: dict[str, dict[str, Any]],
) -> None:
    for call_id, metadata in items.items():
        remember(owner_user_id, connection_id, call_id, metadata)


def lookup(
    owner_user_id: uuid.UUID | str, connection_id: uuid.UUID | str, call_id: str
) -> Optional[dict[str, Any]]:
    entry = _entries.get(_key(owner_user_id, connection_id, call_id))
    if entry is None:
        return None
    stored_at, metadata = entry
    if time.monotonic() - stored_at > CACHE_TTL_SECONDS:
        _entries.pop(_key(owner_user_id, connection_id, call_id), None)
        return None
    return metadata


def forget_owner(owner_user_id: uuid.UUID | str) -> None:
    """Drop everything cached for one owner (used on logout/deactivation)."""
    owner = str(owner_user_id)
    for key in [key for key in _entries if key[0] == owner]:
        _entries.pop(key, None)
