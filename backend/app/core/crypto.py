"""AES-256-GCM encryption for sensitive database fields.

Secrets such as Voximplant Kit access tokens are never stored in plaintext.
Each record carries its own random 96-bit IV plus the GCM auth tag, so the
ciphertext is both confidential and tamper-evident. The master key lives only
in the environment (TOKEN_ENCRYPTION_KEY).
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

CURRENT_KEY_VERSION = 1
_MASK = "\u2022" * 8


class DecryptionError(RuntimeError):
    """Raised when a stored ciphertext cannot be authenticated."""


@dataclass(frozen=True)
class EncryptedPayload:
    ciphertext: str
    iv: str
    auth_tag: str
    key_version: int = CURRENT_KEY_VERSION


def _key() -> bytes:
    return get_settings().encryption_key_bytes


def encrypt_secret(plaintext: str) -> EncryptedPayload:
    """Encrypt a secret with a fresh random IV."""
    if not plaintext:
        raise ValueError("Cannot encrypt an empty secret")

    iv = os.urandom(12)
    sealed = AESGCM(_key()).encrypt(iv, plaintext.encode("utf-8"), None)
    # cryptography appends the 16-byte tag to the ciphertext; store them apart
    # so the schema stays explicit and auditable.
    ciphertext, auth_tag = sealed[:-16], sealed[-16:]

    return EncryptedPayload(
        ciphertext=base64.b64encode(ciphertext).decode("ascii"),
        iv=base64.b64encode(iv).decode("ascii"),
        auth_tag=base64.b64encode(auth_tag).decode("ascii"),
        key_version=CURRENT_KEY_VERSION,
    )


def decrypt_secret(ciphertext: str, iv: str, auth_tag: str) -> str:
    """Decrypt a stored secret. Raises DecryptionError on tampering/key change."""
    try:
        sealed = base64.b64decode(ciphertext) + base64.b64decode(auth_tag)
        return AESGCM(_key()).decrypt(base64.b64decode(iv), sealed, None).decode("utf-8")
    except InvalidTag as exc:
        raise DecryptionError(
            "Stored secret failed authentication (wrong key or tampered data)"
        ) from exc
    except Exception as exc:  # base64 / unicode problems
        raise DecryptionError("Stored secret is malformed") from exc


def mask_secret(plaintext_length: int = 0, tail: str = "") -> str:
    """Build a display-safe mask. Never derived from the real secret value."""
    if tail:
        return f"{_MASK}{tail}"
    return _MASK


def secret_fingerprint(plaintext: str) -> str:
    """Short non-reversible fingerprint, safe for audit logs and UI hints."""
    import hashlib

    digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    return digest[:12]
