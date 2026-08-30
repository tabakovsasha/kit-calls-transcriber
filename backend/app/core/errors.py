"""Domain error types mapped to HTTP responses by the global handler."""

from __future__ import annotations

from typing import Any, Optional


class AppError(Exception):
    """Base application error with a machine-readable code."""

    status_code = 400
    code = "APP_ERROR"

    def __init__(self, message: str, *, code: Optional[str] = None, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details


class AuthError(AppError):
    status_code = 401
    code = "UNAUTHENTICATED"


class ForbiddenError(AppError):
    status_code = 403
    code = "FORBIDDEN"


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"


class ValidationError(AppError):
    status_code = 422
    code = "VALIDATION_FAILED"


class RateLimitError(AppError):
    status_code = 429
    code = "RATE_LIMITED"


class UpstreamError(AppError):
    """Voximplant Kit API failure, classified into a machine code."""

    status_code = 502
    code = "UPSTREAM_UNAVAILABLE"


UPSTREAM_AUTH_FAILED = "UPSTREAM_AUTH_FAILED"
UPSTREAM_FORBIDDEN = "UPSTREAM_FORBIDDEN"
UPSTREAM_VALIDATION_FAILED = "UPSTREAM_VALIDATION_FAILED"
UPSTREAM_RATE_LIMIT = "UPSTREAM_RATE_LIMIT"
UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
UPSTREAM_TRANSPORT = "UPSTREAM_TRANSPORT"
UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
UPSTREAM_MALFORMED_RESPONSE = "UPSTREAM_MALFORMED_RESPONSE"
UPSTREAM_BLOCKED_HOST = "UPSTREAM_BLOCKED_HOST"
