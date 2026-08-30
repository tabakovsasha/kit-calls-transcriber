"""Transcription orchestration: download -> normalize -> inference -> persist.

Credential handling is the important part: the access token is decrypted from
the connection record for the duration of one download and is never written to
the queue row, the transcript row, a log line or a response.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.runtime import runtime
from app.db.models import QueueItemStatus, TranscriptionQueueItem
from app.services import (
    connection_service,
    media_service,
    queue_service,
    transcript_service,
    whisper_service,
)
from app.services.events_service import hub
from app.services.voximplant_client import UpstreamCredentials, VoximplantClient

logger = logging.getLogger(__name__)


async def _publish_snapshot(db: AsyncSession, owner_user_id: uuid.UUID) -> None:
    """Push the current queue state to the owner's live sockets."""
    snapshot = await queue_service.build_snapshot(db, owner_user_id)
    await hub.publish(
        owner_user_id,
        {"type": "queue_snapshot", "snapshot": snapshot.model_dump(mode="json")},
    )


async def transcribe_call(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    connection_id: uuid.UUID,
    call_id: str,
    record_url: str,
    whisper_model: str,
    credentials: UpstreamCredentials,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Transcribe one recording and cache the result.

    Returns a dict with ``transcript``, ``skipped``, ``error`` and ``cached``.
    """
    cached = await transcript_service.get_transcript(
        db, owner_user_id, connection_id, call_id
    )
    if cached is not None and cached.transcript_text:
        return {
            "transcript": cached.transcript_text,
            "skipped": False,
            "error": None,
            "cached": True,
        }

    meta = metadata or {}
    client = VoximplantClient(credentials)
    download_path = media_service.audio_path(connection_id, call_id)

    async with runtime.limiter.slot():
        runtime.active_transcriptions += 1
        try:
            await client.download_audio(record_url, download_path)
            if not download_path.exists() or download_path.stat().st_size == 0:
                raise AppError("Скачанный аудиофайл пустой")

            # Whisper needs 16 kHz mono; normalization also pads tiny clips.
            normalized_path: Path = await asyncio.to_thread(
                whisper_service.normalize_audio, download_path
            )

            model = await whisper_service.get_or_load_model(whisper_model)
            async with whisper_service.maybe_inference_lock(whisper_model):
                runtime.active_inference_tasks += 1
                try:
                    result, transcript = await whisper_service.transcribe_audio(
                        model, normalized_path, whisper_model
                    )
                finally:
                    runtime.active_inference_tasks = max(
                        0, runtime.active_inference_tasks - 1
                    )
        finally:
            runtime.active_transcriptions = max(0, runtime.active_transcriptions - 1)

    if result.get("skipped"):
        return {
            "transcript": "",
            "skipped": True,
            "error": result.get("error") or "Некорректное аудио",
            "cached": False,
        }

    await transcript_service.upsert_transcript(
        db,
        owner_user_id=owner_user_id,
        connection_id=connection_id,
        call_id=call_id,
        transcript_text=transcript,
        whisper_model=whisper_model,
        record_url=record_url,
        language=str(result.get("language") or "") or None,
        audio_filename=media_service.audio_filename(connection_id, call_id),
        duration=meta.get("duration"),
        datetime_start=meta.get("datetime_start"),
        timezone=meta.get("timezone"),
        phone_a=meta.get("phone_a"),
        phone_b=meta.get("phone_b"),
        scenario_name=meta.get("scenario_name"),
    )

    return {"transcript": transcript, "skipped": False, "error": None, "cached": False}


async def process_queue_item(db: AsyncSession, item: TranscriptionQueueItem) -> None:
    """Run one queue item to a terminal state and notify the owner.

    Never raises: a failure is recorded on the row so the runner keeps draining.
    """
    owner_user_id = item.owner_user_id
    await _publish_snapshot(db, owner_user_id)

    if not item.record_url:
        queue_service.mark_failed(
            item, "У звонка нет ссылки на запись. Повторите поиск и добавьте звонок снова."
        )
        await db.commit()
        await _publish_snapshot(db, owner_user_id)
        return

    try:
        _, credentials = await connection_service.resolve_credentials(
            db, item.connection_id, owner_user_id
        )
        outcome = await transcribe_call(
            db,
            owner_user_id=owner_user_id,
            connection_id=item.connection_id,
            call_id=item.call_id,
            record_url=item.record_url,
            whisper_model=item.whisper_model,
            credentials=credentials,
            metadata={
                "duration": item.duration,
                "datetime_start": item.datetime_start,
                "timezone": item.timezone,
                "phone_a": item.caller_a,
                "phone_b": item.caller_b,
                "scenario_name": item.scenario_name,
            },
        )
    except AppError as exc:
        # Domain errors carry safe, user-facing messages.
        queue_service.mark_failed(item, exc.message)
        await db.commit()
        await _publish_snapshot(db, owner_user_id)
        return
    except Exception as exc:
        logger.error(
            "[QUEUE] Ошибка обработки call_id=%s: %s", item.call_id, exc, exc_info=True
        )
        queue_service.mark_failed(item, "Внутренняя ошибка транскрибации")
        await db.commit()
        await _publish_snapshot(db, owner_user_id)
        return

    if outcome["skipped"]:
        queue_service.mark_skipped(item, outcome["error"] or "Транскрибация пропущена")
    else:
        queue_service.mark_done(item, outcome["transcript"])

    item.audio_filename = media_service.audio_filename(item.connection_id, item.call_id)
    await db.commit()
    await db.refresh(item)

    if item.status == QueueItemStatus.DONE:
        await hub.publish(
            owner_user_id,
            {
                "type": "queue_item_done",
                "item": queue_service.to_response(item).model_dump(mode="json"),
            },
        )
    await _publish_snapshot(db, owner_user_id)

