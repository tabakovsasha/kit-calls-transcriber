"""Signed audio playback endpoint.

An ``<audio src>`` element cannot carry an Authorization header, so this route
authenticates via the HMAC-signed grant minted by ``media_service`` rather than
a bearer token. The grant is owner-scoped and short-lived; on a cache miss the
recording is fetched from upstream just-in-time using the connection's decrypted
token, which never leaves the server.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from app.api.deps import SessionDep
from app.core.errors import NotFoundError, ValidationError
from app.services import call_cache, connection_service, media_service
from app.services.voximplant_client import VoximplantClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audio", tags=["audio"])


@router.get("/stream")
async def stream_audio(
    db: SessionDep,
    connection_id: Annotated[str, Query(max_length=64)],
    call_id: Annotated[str, Query(max_length=128)],
    owner: Annotated[str, Query(max_length=64)],
    expires: Annotated[int, Query()],
    sig: Annotated[str, Query(max_length=256)],
) -> FileResponse:
    """Serve a cached recording after validating a signed playback grant."""
    media_service.assert_audio_grant(
        connection_id=connection_id,
        call_id=call_id,
        owner=owner,
        expires=expires,
        signature=sig,
    )

    try:
        owner_uuid = uuid.UUID(owner)
        connection_uuid = uuid.UUID(connection_id)
    except ValueError as exc:
        raise ValidationError("Некорректные параметры ссылки") from exc

    path = media_service.audio_path(connection_uuid, call_id)
    if not path.exists() or path.stat().st_size == 0:
        # Cache miss: fetch on demand using the owner's connection token.
        connection, credentials = await connection_service.resolve_credentials(
            db, connection_uuid, owner_uuid
        )
        metadata = call_cache.lookup(owner_uuid, connection_uuid, call_id)
        record_url = (metadata or {}).get("record_url")
        if not record_url:
            raise NotFoundError(
                "Запись недоступна. Повторите поиск, чтобы обновить ссылку на запись"
            )
        await VoximplantClient(credentials).download_audio(record_url, path)

    if not path.exists() or path.stat().st_size == 0:
        raise NotFoundError("Запись недоступна")

    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename=f"call-{call_id}.mp3",
    )
