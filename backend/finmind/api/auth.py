"""API-key auth — resolves both env-var allowlist (Phase 3 stub) and
real `fck_live_` keys from the `api_keys` table (Phase 4).

Returns an `AuthContext` so downstream dependencies (quota, audit
logging) can tell whether the caller is a billed customer or a
dev-mode / operator session — quota only applies to real keys, since
env-var allowlist users are operators who set the cap themselves.

Two-tier:
  - `require_api_key`    — public read endpoints (data queries)
  - `require_admin_key`  — admin endpoints (toggle enabled, etc.)
                           Single key in `FINMIND_ADMIN_API_KEY` env.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from finmind.billing.keys import KEY_PREFIX, verify_key
from finmind.db.session import get_finmind_db
from finmind.models.billing import ApiKey


@dataclass
class AuthContext:
    """Result of `require_api_key` — captures both the validated key
    string AND (when applicable) the resolved ApiKey row.

    `api_key=None` means the caller authenticated via env-var
    allowlist (operator / dev mode); quota check should skip in that
    path. `api_key=<ApiKey>` means a real billed `fck_live_` key —
    quota check applies."""

    api_key: ApiKey | None
    plaintext: str
    via: str  # 'allowlist' | 'fck_live_key' | 'dev_open'


def _allowlist() -> set[str]:
    raw = os.environ.get("FINMIND_API_KEYS_ALLOWLIST", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def _admin_key() -> str:
    return os.environ.get("FINMIND_ADMIN_API_KEY", "").strip()


async def require_api_key(
    x_finmind_api_key: str | None = Header(default=None),
    db: AsyncSession = Depends(get_finmind_db),
) -> AuthContext:
    """Resolve auth in this priority:

      1. `X-Finmind-API-Key` matches a `fck_live_` prefix → look up
         in `api_keys`, hmac-compare, check enabled + not expired.
         Returns the ApiKey row so quota can charge it.
      2. `X-Finmind-API-Key` matches an entry in
         FINMIND_API_KEYS_ALLOWLIST env var → operator/dev key,
         no quota.
      3. DEBUG=true and allowlist empty → open access (local dev).

    401 on missing or unknown key. The 401 response body intentionally
    avoids saying which path failed so an attacker can't probe whether
    a given prefix is a real key or not.
    """
    if x_finmind_api_key and x_finmind_api_key.startswith(KEY_PREFIX):
        api_key = await verify_key(db, x_finmind_api_key)
        if api_key is not None:
            return AuthContext(
                api_key=api_key,
                plaintext=x_finmind_api_key,
                via="fck_live_key",
            )
        # Don't fall through to allowlist if the prefix matched but
        # the key was invalid — surfaces tampering / expired keys
        # loudly instead of silently re-checking against env vars.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired API key",
        )

    allowlist = _allowlist()
    if not allowlist and os.environ.get("DEBUG", "").lower() in ("true", "1"):
        return AuthContext(
            api_key=None,
            plaintext="<dev-mode-no-auth>",
            via="dev_open",
        )
    if not x_finmind_api_key or x_finmind_api_key not in allowlist:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid X-Finmind-API-Key header",
        )
    return AuthContext(
        api_key=None,
        plaintext=x_finmind_api_key,
        via="allowlist",
    )


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
