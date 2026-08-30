"""Password hashing, JWT access tokens, refresh tokens and signed URLs."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import get_settings

# Argon2id with parameters suitable for an interactive login endpoint.
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)

JWT_ALGORITHM = "HS256"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Passwords --------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time-ish verification that never raises on bad input."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError, TypeError, ValueError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return False


def validate_password_strength(password: str) -> Optional[str]:
    """Return an error message when the password is too weak, else None."""
    settings = get_settings()
    if len(password) < settings.password_min_length:
        return f"Пароль должен содержать минимум {settings.password_min_length} символов"
    if password.lower() == password or password.upper() == password:
        return "Пароль должен содержать буквы в разном регистре"
    if not any(char.isdigit() for char in password):
        return "Пароль должен содержать хотя бы одну цифру"
    return None


# --- Access tokens (JWT) ----------------------------------------------------


def create_access_token(
    user_id: uuid.UUID | str,
    email: str,
    role: str,
    session_id: uuid.UUID | str,
) -> tuple[str, int]:
    """Return (token, expires_in_seconds)."""
    settings = get_settings()
    now = utcnow()
    expires_in = settings.access_token_ttl_seconds
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "sid": str(session_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
        "typ": "access",
    }
    token = jwt.encode(payload, settings.jwt_access_secret, algorithm=JWT_ALGORITHM)
    return token, expires_in


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT. Raises jwt.PyJWTError on failure."""
    settings = get_settings()
    payload = jwt.decode(
        token,
        settings.jwt_access_secret,
        algorithms=[JWT_ALGORITHM],
        options={"require": ["exp", "sub", "sid"]},
    )
    if payload.get("typ") != "access":
        raise jwt.InvalidTokenError("Unexpected token type")
    return payload


# --- Refresh tokens ---------------------------------------------------------


def generate_refresh_token() -> str:
    """High-entropy opaque token; only its hash is stored."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def refresh_token_expiry() -> datetime:
    return utcnow() + timedelta(days=get_settings().refresh_token_ttl_days)


# --- Signed short-lived URLs (audio playback, websocket tickets) ------------


def _sign(message: str) -> str:
    settings = get_settings()
    return hmac.new(
        settings.media_url_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def sign_payload(scope: str, subject: str, owner_user_id: str, ttl_seconds: int) -> tuple[str, int]:
    """Create an HMAC signature for a scoped, expiring resource grant.

    Used where an Authorization header is impossible (``<audio src>``,
    ``new WebSocket()``). Returns (signature, expires_at_epoch).
    """
    expires_at = int((utcnow() + timedelta(seconds=ttl_seconds)).timestamp())
    message = f"{scope}:{subject}:{owner_user_id}:{expires_at}"
    return _sign(message), expires_at


def verify_signed_payload(
    scope: str, subject: str, owner_user_id: str, expires_at: int, signature: str
) -> bool:
    if expires_at < int(utcnow().timestamp()):
        return False
    message = f"{scope}:{subject}:{owner_user_id}:{expires_at}"
    return hmac.compare_digest(_sign(message), signature)


def hash_record_url(record_url: str) -> str:
    """Stable hash used as a cache key without persisting the raw URL."""
    return hashlib.sha256(record_url.encode("utf-8")).hexdigest()
