"""ORM models.

Normalized schema:
- users / user_profiles / user_sessions            -> identity & auth
- voximplant_connections                           -> multi-account per user
- transcription_queue_items / transcripts          -> transcription domain
- transcription_schedules                          -> recurring jobs
- audit_events                                     -> security audit trail

Ownership rule: every domain row carries owner_user_id, and it is always
derived from the authenticated session, never from a client payload.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class UserRole(str, enum.Enum):
    ADMIN = "ADMIN"
    USER = "USER"


class ConnectionStatus(str, enum.Enum):
    UNKNOWN = "unknown"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    INVALID_CREDENTIALS = "invalid_credentials"
    ERROR = "error"


class QueueItemStatus(str, enum.Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELED = "canceled"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    # password_hash holds an Argon2id hash, never a reversible value.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role", native_enum=False, length=16),
        nullable=False,
        default=UserRole.USER,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    profile: Mapped[Optional["UserProfile"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )
    sessions: Mapped[List["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    connections: Mapped[List["VoximplantConnection"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN


class UserProfile(Base, TimestampMixin):
    __tablename__ = "user_profiles"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    first_name: Mapped[Optional[str]] = mapped_column(String(120))
    last_name: Mapped[Optional[str]] = mapped_column(String(120))
    display_name: Mapped[Optional[str]] = mapped_column(String(240))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="ru")
    avatar_url: Mapped[Optional[str]] = mapped_column(String(1024))

    user: Mapped[User] = relationship(back_populates="profile")


class UserSession(Base, TimestampMixin):
    __tablename__ = "user_sessions"
    __table_args__ = (
        Index("ix_user_sessions_owner", "owner_user_id"),
        Index("ix_user_sessions_revoked", "revoked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Only a SHA-256 hash of the refresh token is persisted.
    refresh_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(512))
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="sessions")


class VoximplantConnection(Base, TimestampMixin):
    """One Voximplant Kit account belonging to one user.

    A user may own many connections; exactly one may be marked default.
    The access token is stored as an AES-256-GCM encrypted blob.
    """

    __tablename__ = "voximplant_connections"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "api_host", "domain", name="uq_connection_owner_host_domain"
        ),
        Index("ix_connections_owner", "owner_user_id"),
        Index("ix_connections_owner_default", "owner_user_id", "is_default"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    api_host: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)

    # --- Encrypted access token ---------------------------------------------
    access_token_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_iv: Mapped[str] = mapped_column(String(32), nullable=False)
    access_token_auth_tag: Mapped[str] = mapped_column(String(32), nullable=False)
    access_token_key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    access_token_fingerprint: Mapped[Optional[str]] = mapped_column(String(16))
    access_token_rotated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_connection_status: Mapped[ConnectionStatus] = mapped_column(
        SAEnum(ConnectionStatus, name="connection_status", native_enum=False, length=32),
        nullable=False,
        default=ConnectionStatus.UNKNOWN,
    )
    last_connection_error: Mapped[Optional[str]] = mapped_column(String(512))
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    account_name: Mapped[Optional[str]] = mapped_column(String(255))
    account_id: Mapped[Optional[int]] = mapped_column(Integer)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="connections")


class Transcript(Base, TimestampMixin):
    """Cached transcription result, scoped to owner + connection."""

    __tablename__ = "transcripts"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "connection_id", "call_id", name="uq_transcript_owner_conn_call"
        ),
        Index("ix_transcripts_owner_created", "owner_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("voximplant_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # Hash of the record URL: the URL itself may embed a token.
    record_url_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    transcript_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    whisper_model: Mapped[str] = mapped_column(String(32), nullable=False, default="base")
    language: Mapped[Optional[str]] = mapped_column(String(32))
    audio_filename: Mapped[Optional[str]] = mapped_column(String(255))
    duration: Mapped[Optional[int]] = mapped_column(Integer)
    datetime_start: Mapped[Optional[str]] = mapped_column(String(64))
    timezone: Mapped[Optional[str]] = mapped_column(String(64))
    phone_a: Mapped[Optional[str]] = mapped_column(String(64))
    phone_b: Mapped[Optional[str]] = mapped_column(String(64))
    scenario_name: Mapped[Optional[str]] = mapped_column(String(255))



class TranscriptionQueueItem(Base, TimestampMixin):
    """Durable transcription queue, per owner and per connection."""

    __tablename__ = "transcription_queue_items"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "connection_id", "call_id", name="uq_queue_owner_conn_call"
        ),
        Index("ix_queue_status_created", "status", "created_at"),
        Index("ix_queue_owner_status", "owner_user_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("voximplant_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[QueueItemStatus] = mapped_column(
        SAEnum(QueueItemStatus, name="queue_item_status", native_enum=False, length=16),
        nullable=False,
        default=QueueItemStatus.QUEUED,
    )
    whisper_model: Mapped[str] = mapped_column(String(32), nullable=False, default="base")
    # record_url is an upstream media URL. Tokens are never stored here; they
    # are injected per-request from the encrypted connection record.
    record_url: Mapped[Optional[str]] = mapped_column(Text)
    datetime_start: Mapped[Optional[str]] = mapped_column(String(64))
    timezone: Mapped[Optional[str]] = mapped_column(String(64))
    caller_a: Mapped[Optional[str]] = mapped_column(String(64))
    caller_b: Mapped[Optional[str]] = mapped_column(String(64))
    duration: Mapped[Optional[int]] = mapped_column(Integer)
    scenario_name: Mapped[Optional[str]] = mapped_column(String(255))
    transcript_text: Mapped[Optional[str]] = mapped_column(Text)
    audio_filename: Mapped[Optional[str]] = mapped_column(String(255))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class TranscriptionSchedule(Base, TimestampMixin):
    """Recurring hourly transcription job bound to one connection."""

    __tablename__ = "transcription_schedules"
    __table_args__ = (Index("ix_schedules_owner", "owner_user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("voximplant_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    weekdays: Mapped[list[int]] = mapped_column(JSONB, nullable=False, default=list)
    from_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=9)
    to_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=18)
    scenario_id: Mapped[Optional[int]] = mapped_column(Integer)
    min_duration: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    has_recording: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    records_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    whisper_model: Mapped[str] = mapped_column(String(32), nullable=False, default="base")
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_run_slot: Mapped[Optional[str]] = mapped_column(String(64))
    last_result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)


class AuditEvent(Base):
    """Append-only security/audit trail."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_owner_created", "owner_user_id", "created_at"),
        Index("ix_audit_action_created", "action", "created_at"),
        Index("ix_audit_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    # actor = who performed the action; owner = whose data was affected.
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_email: Mapped[Optional[str]] = mapped_column(String(320))
    owner_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    target_type: Mapped[Optional[str]] = mapped_column(String(64))
    target_id: Mapped[Optional[str]] = mapped_column(String(64))
    target_label: Mapped[Optional[str]] = mapped_column(String(320))
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))
    user_agent: Mapped[Optional[str]] = mapped_column(String(512))
    # Payload is redacted before insert; secrets never reach this column.
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

