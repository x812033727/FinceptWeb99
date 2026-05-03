"""API-key auth — Phase 3 stub.

Reads `X-Finmind-API-Key` header and compares against a static
allowlist sourced from `FINMIND_API_KEYS_ALLOWLIST` env var (comma-
separated). In Phase 4 this gets replaced by real per-user / per-key
records in the billing tables, but the stub keeps the public API
non-trivially gated even in Phase 3 deployments.

Two-tier:
  - `require_api_key` — public read endpoints (data queries)
  - `require_admin_key` — admin endpoints (toggle enabled, etc.)
                          Single key in `FINMIND_ADMIN_API_KEY` env.
"""
from __future__ import annotations

import os

from fastapi import Header, HTTPException, status


def _allowlist() -> set[str]:
    raw = os.environ.get("FINMIND_API_KEYS_ALLOWLIST", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def _admin_key() -> str:
    return os.environ.get("FINMIND_ADMIN_API_KEY", "").strip()


async def require_api_key(
    x_finmind_api_key: str | None = Header(default=None),
) -> str:
    """Returns the validated key string — callers can use it for
    quota / audit later. 401 on missing or unknown key.

    Open-deployment shortcut: if FINMIND_API_KEYS_ALLOWLIST is empty
    AND DEBUG=true, allow unauthenticated requests so local dev
    isn't blocked. Production deployments MUST set the allowlist.
    """
    allowlist = _allowlist()
    if not allowlist and os.environ.get("DEBUG", "").lower() in ("true", "1"):
        return "<dev-mode-no-auth>"
    if not x_finmind_api_key or x_finmind_api_key not in allowlist:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid X-Finmind-API-Key header",
        )
    return x_finmind_api_key


async def require_admin_key(
    x_finmind_admin_key: str | None = Header(default=None),
) -> str:
    expected = _admin_key()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "admin endpoints disabled — set FINMIND_ADMIN_API_KEY "
                "to enable"
            ),
        )
    if x_finmind_admin_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid X-Finmind-Admin-Key header",
        )
    return x_finmind_admin_key
