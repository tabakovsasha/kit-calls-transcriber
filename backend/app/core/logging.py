"""Structured logging with mandatory secret redaction.

Every log record passes through RedactingFilter. Even if a developer
accidentally interpolates a token into a log message, the value is replaced
before it reaches a handler.
"""

from __future__ import annotations

import logging
import re
import warnings
from typing import Any, Iterable

REDACTED = "[REDACTED]"

# key=value / "key": "value" / key: value forms for sensitive names.
_SENSITIVE_KEYS = (
    "access_token",
    "accesstoken",
    "refresh_token",
    "refreshtoken",
    "password",
    "passwordhash",
    "password_hash",
    "token_encryption_key",
    "jwt_access_secret",
    "media_url_secret",
    "authorization",
    "secret",
    "api_key",
    "apikey",
)

_KV_PATTERN = re.compile(
    r"(?i)\b(" + "|".join(_SENSITIVE_KEYS) + r")\b(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}&)]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+")
_QUERY_TOKEN_PATTERN = re.compile(
    r"(?i)([?&](?:access_token|token|sig|ticket)=)[^&\s]+"
)


def redact(text: str) -> str:
    """Replace secret-looking values inside an arbitrary string."""
    if not text:
        return text
    text = _KV_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _BEARER_PATTERN.sub(f"Bearer {REDACTED}", text)
    text = _QUERY_TOKEN_PATTERN.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    return text


def redact_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy a dict with sensitive values replaced (for audit payloads)."""
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        if key.lower().replace("-", "_") in _SENSITIVE_KEYS:
            safe[key] = REDACTED
        elif isinstance(value, dict):
            safe[key] = redact_mapping(value)
        elif isinstance(value, str):
            safe[key] = redact(value)
        else:
            safe[key] = value
    return safe


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        key: redact(value) if isinstance(value, str) else value
                        for key, value in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        redact(value) if isinstance(value, str) else value
                        for value in record.args
                    )
        except Exception:
            # Logging must never break the request path.
            return True
        return True


def configure_logging(level: int = logging.INFO, extra_loggers: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    )
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    for name in extra_loggers:
        logging.getLogger(name).addFilter(RedactingFilter())

    warnings.filterwarnings("ignore", message=".*FP16 is not supported on CPU.*")
