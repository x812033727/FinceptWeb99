"""Symmetric encryption for LLM provider API keys at rest.

Keys are stored Fernet-encrypted in the `llm_provider_keys` table. The Fernet
key is derived deterministically from JWT_SECRET_KEY via SHA-256, so an
existing deployment doesn't need a new env var to start using this feature.

Operators who want a separate rotation lifecycle can override by setting
LLM_KEY_ENCRYPTION_KEY explicitly (any string ≥ 32 chars). Rotating either
secret invalidates all previously-stored encrypted keys — admins must re-enter
them through the UI.
"""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from config import settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    raw = (settings.LLM_KEY_ENCRYPTION_KEY or settings.JWT_SECRET_KEY).encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> str:
    """Encrypt and return a base64 string suitable for a Text DB column."""
    if not plaintext:
        raise ValueError("cannot encrypt empty string")
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str | None:
    """Decrypt; return None if the token is malformed or signed by a different key.

    None means 'caller should fall back to the .env-supplied key' rather than
    crashing the request — useful when JWT_SECRET_KEY rotates after rows are
    written and operators haven't re-entered keys yet.
    """
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def mask(plaintext: str) -> str:
    """Return a UI-safe preview: last 4 chars only, prefixed with bullets."""
    if not plaintext:
        return ""
    n = len(plaintext)
    if n <= 4:
        return "•" * n
    return "•" * 8 + plaintext[-4:]
