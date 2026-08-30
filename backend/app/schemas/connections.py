"""Voximplant connection schemas.

The access token is write-only: it enters through create/rotate requests and
is never present in any response model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.db.models import ConnectionStatus
from app.schemas.common import ORMModel


class ConnectionCreateRequest(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    api_host: str = Field(min_length=4, max_length=255)
    domain: str = Field(min_length=1, max_length=255)
    access_token: str = Field(min_length=8, max_length=4096)
    is_default: bool = False
    verify: bool = True

    @field_validator("api_host", "domain", "label")
    @classmethod
    def strip_value(cls, value: str) -> str:
        return value.strip()


class ConnectionUpdateRequest(BaseModel):
    label: Optional[str] = Field(default=None, min_length=1, max_length=120)
    api_host: Optional[str] = Field(default=None, min_length=4, max_length=255)
    domain: Optional[str] = Field(default=None, min_length=1, max_length=255)
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None

    @field_validator("api_host", "domain", "label")
    @classmethod
    def strip_optional(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if isinstance(value, str) else value


class ConnectionRotateTokenRequest(BaseModel):
    access_token: str = Field(min_length=8, max_length=4096)
    verify: bool = True


class ConnectionResponse(ORMModel):
    id: UUID
    label: str
    api_host: str
    domain: str
    is_active: bool
    is_default: bool
    last_connection_status: ConnectionStatus
    last_connection_error: Optional[str] = None
    last_checked_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None
    account_name: Optional[str] = None
    account_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    access_token_rotated_at: Optional[datetime] = None
    # Display-safe only: a mask and a short non-reversible fingerprint.
    access_token_masked: str = ""
    access_token_fingerprint: Optional[str] = None
    owner_user_id: Optional[UUID] = None
    owner_email: Optional[str] = None


class ConnectionVerifyResponse(BaseModel):
    connection_id: UUID
    status: ConnectionStatus
    checked_at: datetime
    error: Optional[str] = None
