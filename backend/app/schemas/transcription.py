"""Transcription domain schemas: search, queue, schedules, transcripts.

Credentials are absent from every payload: the backend resolves them from the
caller's selected connection, so a client can never inject a foreign token.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.db.models import QueueItemStatus
from app.schemas.common import ORMModel

SUPPORTED_WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3", "turbo"]
DEFAULT_WHISPER_MODEL = "base"


def normalize_whisper_model_name(model_name: Optional[str]) -> str:
    name = (model_name or DEFAULT_WHISPER_MODEL).strip().lower()
    return name if name in SUPPORTED_WHISPER_MODELS else DEFAULT_WHISPER_MODEL


class CallSearchRequest(BaseModel):
    connection_id: UUID
    from_date: str = Field(min_length=10, max_length=32)
    to_date: str = Field(min_length=10, max_length=32)
    scenario_id: Optional[int] = None
    min_duration: int = Field(default=0, ge=0, le=86400)
    has_recording: bool = True
    records_limit: int = Field(default=100, ge=1, le=5000)


class CallRecord(BaseModel):
    id: str
    datetime_start: Optional[str] = None
    timezone: Optional[str] = None
    phone_a: Optional[str] = None
    phone_b: Optional[str] = None
    duration: int = 0
    scenario_name: Optional[str] = None
    has_recording: bool = False
    transcript: Optional[str] = None
    # Signed, short-lived playback URL. Never an upstream URL with a token.
    audio_url: Optional[str] = None


class CallSearchResponse(BaseModel):
    items: List[CallRecord]
    total_loaded: int
    can_load_more: bool
    cursor: Optional[str] = None
    clamped_to_now: bool = False


class QueueAddRequest(BaseModel):
    connection_id: UUID
    call_ids: List[str] = Field(min_length=1, max_length=500)
    # None means "use the persisted default": the model is a server-side setting,
    # so a client that does not care must not pin itself to a hardcoded value.
    whisper_model: Optional[str] = None

    @field_validator("whisper_model")
    @classmethod
    def check_model(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else normalize_whisper_model_name(value)


class QueueClearRequest(BaseModel):
    statuses: List[QueueItemStatus] = Field(
        default_factory=lambda: [
            QueueItemStatus.DONE,
            QueueItemStatus.FAILED,
            QueueItemStatus.SKIPPED,
        ]
    )


class QueueItemResponse(ORMModel):
    id: UUID
    connection_id: UUID
    call_id: str
    status: QueueItemStatus
    whisper_model: str
    datetime_start: Optional[str] = None
    timezone: Optional[str] = None
    caller_a: Optional[str] = None
    caller_b: Optional[str] = None
    duration: Optional[int] = None
    scenario_name: Optional[str] = None
    transcript_text: Optional[str] = None
    error_message: Optional[str] = None
    attempts: int = 0
    created_at: datetime
    updated_at: datetime
    audio_url: Optional[str] = None


class QueueCounters(BaseModel):
    total: int = 0
    queued: int = 0
    processing: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    canceled: int = 0


class QueueSnapshot(BaseModel):
    items: List[QueueItemResponse]
    counters: QueueCounters
    is_running: bool = False


class QueueAddResponse(BaseModel):
    """Outcome of a batch enqueue.

    The snapshot alone cannot tell the user what happened to *their* request:
    an idempotent add silently leaves already-active calls alone. These counts
    make the result explainable ("added 10, skipped 2, because ...").
    """

    queued: int = 0
    # Already queued or processing, so re-adding would duplicate work.
    skipped_active: List[str] = Field(default_factory=list)
    # Metadata (and therefore the record URL) fell out of the server-side cache.
    skipped_expired: List[str] = Field(default_factory=list)
    snapshot: QueueSnapshot


class ScheduleCreateRequest(BaseModel):
    connection_id: UUID
    name: str = Field(default="", max_length=255)
    weekdays: List[int] = Field(min_length=1, max_length=7)
    from_hour: int = Field(ge=0, le=23)
    to_hour: int = Field(ge=0, le=23)
    scenario_id: Optional[int] = None
    min_duration: int = Field(default=0, ge=0, le=86400)
    has_recording: bool = True
    records_limit: int = Field(default=100, ge=1, le=5000)
    whisper_model: str = DEFAULT_WHISPER_MODEL
    enabled: bool = True

    @field_validator("weekdays")
    @classmethod
    def sanitize_weekdays(cls, value: List[int]) -> List[int]:
        cleaned = sorted({int(day) for day in value if 0 <= int(day) <= 6})
        if not cleaned:
            raise ValueError("Нужно выбрать хотя бы один день недели")
        return cleaned

    @field_validator("whisper_model")
    @classmethod
    def check_model(cls, value: str) -> str:
        return normalize_whisper_model_name(value)


class ScheduleUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    enabled: Optional[bool] = None
    weekdays: Optional[List[int]] = None
    from_hour: Optional[int] = Field(default=None, ge=0, le=23)
    to_hour: Optional[int] = Field(default=None, ge=0, le=23)
    min_duration: Optional[int] = Field(default=None, ge=0, le=86400)
    has_recording: Optional[bool] = None
    records_limit: Optional[int] = Field(default=None, ge=1, le=5000)
    whisper_model: Optional[str] = None


class ScheduleResponse(ORMModel):
    id: UUID
    connection_id: UUID
    name: str
    enabled: bool
    weekdays: List[int]
    from_hour: int
    to_hour: int
    scenario_id: Optional[int] = None
    min_duration: int
    has_recording: bool
    records_limit: int
    whisper_model: str
    last_run_at: Optional[datetime] = None
    last_run_slot: Optional[str] = None
    last_result: Optional[dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime


class TranscriptResponse(ORMModel):
    id: UUID
    connection_id: UUID
    call_id: str
    transcript_text: str
    whisper_model: str
    language: Optional[str] = None
    duration: Optional[int] = None
    datetime_start: Optional[str] = None
    timezone: Optional[str] = None
    phone_a: Optional[str] = None
    phone_b: Optional[str] = None
    scenario_name: Optional[str] = None
    created_at: datetime


class ScenarioItem(BaseModel):
    id: Optional[int] = None
    title: Optional[str] = None


class AudioUrlRequest(BaseModel):
    connection_id: UUID
    call_id: str = Field(min_length=1, max_length=128)


class AudioUrlResponse(BaseModel):
    audio_url: str
    expires_at: int
