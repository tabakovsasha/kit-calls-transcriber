"""Transcript cache, scoped to owner + connection.

Replaces the legacy ``transcripts-cache.json``. The record URL is stored only
as a hash because the raw URL embeds an access token.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_record_url
from app.db.models import Transcript

logger = logging.getLogger(__name__)


async def get_transcript(
    db: AsyncSession,
    owner_user_id: uuid.UUID,
    connection_id: uuid.UUID,
    call_id: str,
) -> Optional[Transcript]:
    result = await db.execute(
        select(Transcript).where(
            Transcript.owner_user_id == owner_user_id,
            Transcript.connection_id == connection_id,
            Transcript.call_id == call_id,
        )
    )
    return result.scalar_one_or_none()


async def get_transcript_map(
    db: AsyncSession,
    owner_user_id: uuid.UUID,
    connection_id: uuid.UUID,
    call_ids: Sequence[str],
) -> dict[str, str]:
    """Bulk-load cached transcripts so a search result can be enriched in one query."""
    if not call_ids:
        return {}
    result = await db.execute(
        select(Transcript.call_id, Transcript.transcript_text).where(
            Transcript.owner_user_id == owner_user_id,
            Transcript.connection_id == connection_id,
            Transcript.call_id.in_(list(call_ids)),
        )
    )
    return {row[0]: row[1] for row in result.all() if row[1]}


async def list_transcripts(
    db: AsyncSession,
    owner_user_id: uuid.UUID,
    *,
    connection_id: Optional[uuid.UUID] = None,
    limit: int = 200,
) -> Sequence[Transcript]:
    stmt = select(Transcript).where(Transcript.owner_user_id == owner_user_id)
    if connection_id is not None:
        stmt = stmt.where(Transcript.connection_id == connection_id)
    result = await db.execute(stmt.order_by(Transcript.created_at.desc()).limit(limit))
    return result.scalars().all()


async def upsert_transcript(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    connection_id: uuid.UUID,
    call_id: str,
    transcript_text: str,
    whisper_model: str,
    record_url: Optional[str] = None,
    language: Optional[str] = None,
    audio_filename: Optional[str] = None,
    duration: Optional[int] = None,
    datetime_start: Optional[str] = None,
    timezone: Optional[str] = None,
    phone_a: Optional[str] = None,
    phone_b: Optional[str] = None,
    scenario_name: Optional[str] = None,
) -> Transcript:
    """Insert or refresh a cached transcript. Caller commits."""
    transcript = await get_transcript(db, owner_user_id, connection_id, call_id)
    record_hash = hash_record_url(record_url) if record_url else None

    if transcript is None:
        transcript = Transcript(
            owner_user_id=owner_user_id,
            connection_id=connection_id,
            call_id=call_id,
        )
        db.add(transcript)

    transcript.transcript_text = transcript_text
    transcript.whisper_model = whisper_model
    transcript.record_url_hash = record_hash
    transcript.language = language
    transcript.audio_filename = audio_filename
    transcript.duration = duration
    transcript.datetime_start = datetime_start
    transcript.timezone = timezone
    transcript.phone_a = phone_a
    transcript.phone_b = phone_b
    transcript.scenario_name = scenario_name

    await db.flush()
    return transcript
