"""Auth schemas. Passwords are input-only and never serialized back."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.db.models import UserRole
from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class ProfileSummary(ORMModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    display_name: Optional[str] = None
    timezone: str = "UTC"
    language: str = "ru"
    avatar_url: Optional[str] = None


class CurrentUser(ORMModel):
    id: UUID
    email: EmailStr
    role: UserRole
    is_active: bool
    must_change_password: bool
    last_login_at: Optional[datetime] = None
    profile: Optional[ProfileSummary] = None


class TokenResponse(BaseModel):
    """Access token is returned in the body only; refresh lives in a cookie."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    user: CurrentUser


class SessionInfo(ORMModel):
    id: UUID
    user_agent: Optional[str] = None
    ip_address: Optional[str] = None
    created_at: datetime
    last_used_at: Optional[datetime] = None
    expires_at: datetime


class WsTicketResponse(BaseModel):
    ticket: str
    expires_at: int
