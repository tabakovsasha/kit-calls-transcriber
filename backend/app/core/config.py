"""Application settings loaded from environment variables.

Security rules enforced here:
- secrets have no defaults, the app refuses to start without them;
- secrets are never logged or exposed through the API.
"""

from __future__ import annotations

import binascii
from functools import lru_cache
from pathlib import Path
from typing import List, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py lives at backend/app/core/config.py, so three parents reach the
# backend/ directory where .env is stored.
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent



class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- General ------------------------------------------------------------
    app_env: Literal["development", "staging", "production"] = "development"
    app_name: str = "Voximplant Kit Calls Transcriber"
    public_base_url: str = "http://localhost:8000"
    cors_origins: str = "http://localhost:5173"

    # --- Database -----------------------------------------------------------
    database_url: str = (
        "postgresql+asyncpg://kitapp:kitapp@localhost:5432/kit_calls_transcriber"
    )

    # --- Secrets (no defaults on purpose) -----------------------------------
    token_encryption_key: str = Field(min_length=64, max_length=64)
    jwt_access_secret: str = Field(min_length=32)
    media_url_secret: str = Field(min_length=32)

    # --- Session policy -----------------------------------------------------
    access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    refresh_token_ttl_days: int = Field(default=30, ge=1, le=365)
    refresh_cookie_name: str = "kit_refresh"
    refresh_cookie_secure: bool = True
    refresh_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    max_active_sessions: int = Field(default=10, ge=1, le=100)
    login_rate_limit_attempts: int = Field(default=10, ge=1, le=100)
    login_rate_limit_window_seconds: int = Field(default=300, ge=30, le=3600)
    media_url_ttl_seconds: int = Field(default=900, ge=30, le=86400)
    ws_ticket_ttl_seconds: int = Field(default=60, ge=10, le=600)
    password_min_length: int = Field(default=10, ge=8, le=128)

    # --- Bootstrap admin ----------------------------------------------------
    init_admin_email: str = "admin@example.com"
    init_admin_password: str = ""
    init_admin_must_change_password: bool = True

    # --- Upstream protection ------------------------------------------------
    allow_private_upstream_hosts: bool = False
    voximplant_host_allowlist: str = ""

    # --- Transcription runtime ---------------------------------------------
    transcribe_performance_profile: Literal["max", "moderate"] = "moderate"
    whisper_inference_isolation: Literal["serialized", "concurrent"] = "serialized"
    whisper_transcribe_timeout_seconds: int = Field(default=420, ge=30, le=3600)
    whisper_fallback_timeout_seconds: int = Field(default=240, ge=30, le=3600)
    scheduler_poll_seconds: int = Field(default=30, ge=5, le=3600)
    max_transcribe_concurrency: int | None = None
    torch_num_threads: int | None = None

    @field_validator("token_encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: str) -> str:
        try:
            raw = binascii.unhexlify(value)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                "TOKEN_ENCRYPTION_KEY must be 64 hex characters (32 bytes)"
            ) from exc
        if len(raw) != 32:
            raise ValueError("TOKEN_ENCRYPTION_KEY must decode to exactly 32 bytes")
        return value

    @field_validator("max_transcribe_concurrency", "torch_num_threads", mode="before")
    @classmethod
    def empty_string_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def cors_origin_list(self) -> List[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def upstream_host_allowlist(self) -> List[str]:
        return [
            item.strip().lower()
            for item in self.voximplant_host_allowlist.split(",")
            if item.strip()
        ]

    @property
    def encryption_key_bytes(self) -> bytes:
        return binascii.unhexlify(self.token_encryption_key)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
