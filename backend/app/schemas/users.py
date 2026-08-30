"""User and profile schemas (admin management + self-service)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.db.models import UserRole
from app.schemas.auth import ProfileSummary
from app.schemas.common import ORMModel


class UserCreateRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    role: UserRole = UserRole.USER
    is_active: bool = True
    must_change_password: bool = True
    first_name: Optional[str] = Field(default=None, max_length=120)
    last_name: Optional[str] = Field(default=None, max_length=120)


class UserUpdateRequest(BaseModel):
    """Admin-side update. Role/activation changes are audited."""

    email: Optional[EmailStr] = None
    role: Optional[UserRole] = None
    is_active: Optional[bool] = None
    must_change_password: Optional[bool] = None


class AdminResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=256)
    must_change_password: bool = True


class ProfileUpdateRequest(BaseModel):
    first_name: Optional[str] = Field(default=None, max_length=120)
    last_name: Optional[str] = Field(default=None, max_length=120)
    display_name: Optional[str] = Field(default=None, max_length=240)
    timezone: Optional[str] = Field(default=None, max_length=64)
    language: Optional[str] = Field(default=None, max_length=8)
    avatar_url: Optional[str] = Field(default=None, max_length=1024)


class UserResponse(ORMModel):
    id: UUID
    email: EmailStr
    role: UserRole
    is_active: bool
    must_change_password: bool
    created_at: datetime
    updated_at: datetime
    last_login_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    profile: Optional[ProfileSummary] = None
    connections_count: int = 0
