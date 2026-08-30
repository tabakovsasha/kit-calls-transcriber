"""Call history routes: search, scenarios and signed audio playback.

Credentials never appear in a request or a response. The caller names a
connection they own; the backend decrypts that connection's token for the
duration of one upstream call. Upstream record URLs stay server-side and the
browser only ever receives short-lived, HMAC-signed playback URLs.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Query

from app.api.deps import ActiveUserDep, OwnerIdDep, SessionDep
from app.schemas.transcription import (
    AudioUrlRequest,
    AudioUrlResponse,
    CallSearchRequest,
    CallSearchResponse,
    ScenarioItem,
    TranscriptResponse,
)
from app.services import (
    call_cache,
    call_search_service,
    connection_service,
    media_service,
    transcript_service,
)
from app.services.voximplant_client import VoximplantClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/calls", tags=["calls"])


@router.post("/search", response_model=CallSearchResponse)
async def search_calls(
    payload: CallSearchRequest,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
    cursor: Annotated[Optional[str], Query(max_length=512)] = None,
) -> CallSearchResponse:
    """Search call history for one connection, merging cached transcripts."""
    connection, credentials = await connection_service.resolve_credentials(
        db, payload.connection_id, owner_id
    )

    metadata: dict[str, dict[str, Any]] = {}
    result = await call_search_service.search_calls(
        connection=connection,
        credentials=credentials,
        owner_user_id=owner_id,
        from_date=payload.from_date,
        to_date=payload.to_date,
        scenario_id=payload.scenario_id,
        min_duration=payload.min_duration,
        has_recording=payload.has_recording,
        records_limit=payload.records_limit,
        cursor=cursor,
        metadata_sink=metadata,
    )

    # Keep record URLs server-side so the queue can use them later without the
    # client ever seeing or supplying one.
    call_cache.remember_many(owner_id, connection.id, metadata)

    transcripts = await transcript_service.get_transcript_map(
        db, owner_id, connection.id, [item.id for item in result.items]
    )
    for item in result.items:
        if item.transcript is None:
            item.transcript = transcripts.get(item.id)

    return result


@router.get("/scenarios", response_model=list[ScenarioItem])
async def list_scenarios(
    connection_id: uuid.UUID,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> list[ScenarioItem]:
    """Scenario list for the filter dropdown."""
    _, credentials = await connection_service.resolve_credentials(
        db, connection_id, owner_id
    )
    scenarios = await VoximplantClient(credentials).search_scenarios()
    return [ScenarioItem(**item) for item in scenarios]


@router.get("/transcripts", response_model=list[TranscriptResponse])
async def list_transcripts(
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
    connection_id: Optional[uuid.UUID] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[TranscriptResponse]:
    transcripts = await transcript_service.list_transcripts(
        db, owner_id, connection_id=connection_id, limit=limit
    )
    return [TranscriptResponse.model_validate(item) for item in transcripts]


@router.post("/audio-url", response_model=AudioUrlResponse)
async def create_audio_url(
    payload: AudioUrlRequest,
    user: ActiveUserDep,
    owner_id: OwnerIdDep,
    db: SessionDep,
) -> AudioUrlResponse:
    """Issue a signed, expiring playback URL for a recording the caller owns."""
    connection = await connection_service.get_owned_connection(
        db, payload.connection_id, owner_id
    )
    audio_url, expires_at = media_service.build_audio_url(
        owner_id, connection.id, payload.call_id
    )
    return AudioUrlResponse(audio_url=audio_url, expires_at=expires_at)
