"""Schemas for admin-managed runtime settings.

Bodies rather than query parameters: these are state-changing writes, and the
model/profile name belongs in the payload like every other mutation in this API.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from app.core.runtime import SUPPORTED_PROFILES
from app.schemas.transcription import (
    SUPPORTED_WHISPER_MODELS,
    normalize_whisper_model_name,
)


class DefaultModelRequest(BaseModel):
    """Set the default Whisper model used for newly queued items."""

    whisper_model: str = Field(min_length=1, max_length=64)

    @field_validator("whisper_model")
    @classmethod
    def check_model(cls, value: str) -> str:
        # Reuse the queue's normalizer so both paths accept exactly the same set.
        return normalize_whisper_model_name(value)


class PerformanceProfileRequest(BaseModel):
    """Switch the CPU/RAM performance profile."""

    profile: str = Field(min_length=1, max_length=32)

    @field_validator("profile")
    @classmethod
    def check_profile(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in SUPPORTED_PROFILES:
            raise ValueError(
                f"Недопустимый профиль. Доступны: {', '.join(SUPPORTED_PROFILES)}"
            )
        return normalized


class WhisperModelStatus(BaseModel):
    name: str
    size_mb: int
    status: str
    progress: int = 0
    error: Optional[str] = None
    cached: bool = False
    is_default: bool = False


class WhisperModelsResponse(BaseModel):
    models: List[WhisperModelStatus]
    default_model: str
    supported: List[str] = Field(default_factory=lambda: list(SUPPORTED_WHISPER_MODELS))


class RuntimeSettingsResponse(BaseModel):
    """Current effective settings plus the plan they produced."""

    default_model: str
    profile: str
    supported_models: List[str] = Field(
        default_factory=lambda: list(SUPPORTED_WHISPER_MODELS)
    )
    supported_profiles: List[str] = Field(default_factory=lambda: list(SUPPORTED_PROFILES))
    runtime_plan: Dict[str, Any]
    # Precomputed plans for both profiles with the current model, so the UI can
    # show "Optimal" vs "Maximum" without guessing.
    moderate_plan: Dict[str, Any] = Field(default_factory=dict)
    max_plan: Dict[str, Any] = Field(default_factory=dict)


class ModelActionResponse(BaseModel):
    """Result of a download/delete request on one model."""

    whisper_model: str
    success: bool
    status: Optional[Dict[str, Any]] = None
    deleted: Optional[List[str]] = None
    error: Optional[str] = None
