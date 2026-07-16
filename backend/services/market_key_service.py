"""Market-data provider key management — DB-first, .env fallback.

Parallel to `services/llm_key_service.py` but for market connectors.

Public API:
  - resolve_key(provider) → str           (used by data/* connectors)
  - list_keys(db)         → list[KeyInfo] (admin UI list)
  - upsert_key(db, …)     → KeyInfo       (admin UI save)
  - delete_key(db, …)     → None          (admin UI clear)
  - validate_key(provider, key) → ValidationResult  (test button)

Lookup order on each call:
  1. DB row for the provider, decrypted (Fernet)
  2. settings.<PROVIDER>_API_KEY
  3. "" — connector treats as disabled

resolve_key opens its own short-lived AsyncSession because connectors
are called from background polling loops + ad-hoc request handlers and
threading a `db` parameter through every connector signature would be
invasive. The result is cached in-process for `_KEY_CACHE_TTL_S` seconds
so the screener fan-out (25 in flight in batch mode) doesn't hammer the
DB. Admin-side `upsert_key` / `delete_key` invalidate the cache
immediately so the next request sees the new value without waiting for
TTL expiry.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.llm_key_crypto import decrypt, encrypt, mask
from config import settings
from models.market_provider_key import MarketProviderKey

logger = logging.getLogger(__name__)

# Providers exposed in the admin UI. Add new market connectors here as
# they become DB-managed. Keeping it explicit avoids accidentally exposing
# a typo'd provider name through the admin endpoint.
SUPPORTED_PROVIDERS: tuple[str, ...] = ("finnhub", "finmind", "fred")

_ENV_KEY_ATTR = {
    "finnhub": "FINNHUB_API_KEY",
    "finmind": "FINMIND_TOKEN",
    "fred":    "FRED_API_KEY",
}

# In-process cache to avoid hitting the DB on every connector call. The
# TTL is short enough that even forgetting to invalidate would heal in
# under a minute. Admin writes call _invalidate_cache for instant pick-up.
_KEY_CACHE_TTL_S = 60.0
_key_cache: dict[str, tuple[str, float]] = {}


def _invalidate_cache(provider: str | None = None) -> None:
    if provider is None:
        _key_cache.clear()
    else:
        _key_cache.pop(provider, None)


@dataclass
class KeyInfo:
    provider: str
    has_key: bool
    source: Literal["db", "env", "none"]
    masked: str
    last_validated_at: datetime | None
    last_validation_ok: bool | None
    last_validation_message: str | None
    updated_at: datetime | None


@dataclass
class ValidationResult:
    ok: bool
    message: str


def _env_fallback(provider: str) -> str:
    attr = _ENV_KEY_ATTR.get(provider, "")
    return getattr(settings, attr, "") if attr else ""


async def resolve_key(provider: str) -> str:
    """Return the active key for a market-data provider.

    Lookup: in-process cache → DB row → settings env attr → "".
    Empty string is the canonical "disabled" return (matches the
    connector `_enabled()` checks). Never raises — DB / Fernet failures
    fall through to the env value or "" so a momentary outage doesn't
    cascade into the request handler.
    """
    cached = _key_cache.get(provider)
    if cached and cached[1] > time.monotonic():
        return cached[0]

    key = ""
    try:
        from db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            row = await db.get(MarketProviderKey, provider)
            if row is not None:
                plain = decrypt(row.encrypted_key)
                if plain:
                    key = plain
                else:
                    logger.warning("market_key.decrypt_failed",
                                   extra={"provider": provider})
    except Exception as exc:
        logger.warning("market_key.db_lookup_failed",
                       extra={"provider": provider, "error": str(exc)})

    if not key:
        key = _env_fallback(provider)

    _key_cache[provider] = (key, time.monotonic() + _KEY_CACHE_TTL_S)
    return key


async def list_keys(db: AsyncSession) -> list[KeyInfo]:
    """One KeyInfo per supported provider, regardless of whether a row
    exists — frontend renders an empty input for unconfigured providers."""
    rows = (await db.execute(select(MarketProviderKey))).scalars().all()
    by_provider = {r.provider: r for r in rows}

    out: list[KeyInfo] = []
    for prov in SUPPORTED_PROVIDERS:
        row = by_provider.get(prov)
        env_val = _env_fallback(prov)

        if row is not None:
            plain = decrypt(row.encrypted_key) or ""
            out.append(KeyInfo(
                provider=prov,
                has_key=bool(plain),
                source="db" if plain else "none",
                masked=mask(plain) if plain else "(decrypt failed)",
                last_validated_at=row.last_validated_at,
                last_validation_ok=row.last_validation_ok,
                last_validation_message=row.last_validation_message,
                updated_at=row.updated_at,
            ))
        elif env_val:
            out.append(KeyInfo(
                provider=prov, has_key=True, source="env", masked=mask(env_val),
                last_validated_at=None, last_validation_ok=None,
                last_validation_message=None, updated_at=None,
            ))
        else:
            out.append(KeyInfo(
                provider=prov, has_key=False, source="none", masked="",
                last_validated_at=None, last_validation_ok=None,
                last_validation_message=None, updated_at=None,
            ))
    return out


async def upsert_key(
    db: AsyncSession, provider: str, plaintext: str, updated_by_id: uuid.UUID,
) -> KeyInfo:
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported provider: {provider}")
    if not plaintext.strip():
        raise ValueError("key cannot be blank")

    encrypted = encrypt(plaintext.strip())
    row = await db.get(MarketProviderKey, provider)
    if row is None:
        row = MarketProviderKey(
            provider=provider,
            encrypted_key=encrypted,
            updated_by_id=updated_by_id,
        )
        db.add(row)
    else:
        row.encrypted_key = encrypted
        row.updated_by_id = updated_by_id
        row.last_validated_at = None
        row.last_validation_ok = None
        row.last_validation_message = None
    await db.commit()
    await db.refresh(row)
    _invalidate_cache(provider)

    return KeyInfo(
        provider=provider, has_key=True, source="db", masked=mask(plaintext),
        last_validated_at=row.last_validated_at,
        last_validation_ok=row.last_validation_ok,
        last_validation_message=row.last_validation_message,
        updated_at=row.updated_at,
    )


async def delete_key(db: AsyncSession, provider: str) -> None:
    row = await db.get(MarketProviderKey, provider)
    if row is not None:
        await db.delete(row)
        await db.commit()
    _invalidate_cache(provider)


async def record_validation(
    db: AsyncSession, provider: str, result: ValidationResult,
) -> None:
    row = await db.get(MarketProviderKey, provider)
    if row is None:
        return
    row.last_validated_at = datetime.now(UTC)
    row.last_validation_ok = result.ok
    row.last_validation_message = result.message[:500]
    await db.commit()


# ── Validation pings ─────────────────────────────────────────────

_VALIDATE_TIMEOUT = 10.0


async def validate_key(provider: str, key: str) -> ValidationResult:
    if not key.strip():
        return ValidationResult(False, "key is empty")
    try:
        if provider == "finnhub":
            return await _validate_finnhub(key)
        if provider == "finmind":
            return await _validate_finmind(key)
        if provider == "fred":
            return await _validate_fred(key)
        return ValidationResult(False, f"validation not implemented for {provider}")
    except Exception as exc:
        logger.warning("market_key.validate_failed",
                       extra={"provider": provider, "error": str(exc)})
        return ValidationResult(False, f"validation error: {exc}")


async def _validate_fred(key: str) -> ValidationResult:
    """Hit FRED's `/series` endpoint with FEDFUNDS — a series that
    every plan can read. 200 + a `seriess` array means the key works.
    400/401 means rejected; 429 is rate limit.
    """
    async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT) as client:
        r = await client.get(
            "https://api.stlouisfed.org/fred/series",
            params={
                "series_id": "FEDFUNDS",
                "api_key": key,
                "file_type": "json",
            },
        )
    if r.status_code == 200:
        try:
            payload = r.json()
        except (ValueError, json.JSONDecodeError):
            return ValidationResult(False, "non-JSON response")
        if isinstance(payload, dict) and payload.get("seriess"):
            return ValidationResult(True, "OK")
        return ValidationResult(False, "key rejected (empty series response)")
    if r.status_code in (400, 401, 403):
        # FRED returns 400 with `error_message` on bad keys.
        try:
            err = (r.json().get("error_message") or "")[:120]
        except Exception:
            err = ""
        return ValidationResult(
            False,
            f"key rejected by FRED{(' — ' + err) if err else ''}",
        )
    if r.status_code == 429:
        return ValidationResult(False, "rate limit hit — try again shortly")
    return ValidationResult(False, f"HTTP {r.status_code}: {r.text[:200]}")


async def _validate_finnhub(key: str) -> ValidationResult:
    """Hit /quote?symbol=AAPL — guaranteed to be supported on every plan
    and the same endpoint the connector actually uses, so a 200 here
    confirms both the key works and the network path works."""
    async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT) as client:
        r = await client.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": "AAPL", "token": key},
        )
    if r.status_code == 200:
        try:
            payload = r.json()
        except (ValueError, json.JSONDecodeError):
            return ValidationResult(False, "non-JSON response")
        # Finnhub returns 200 + {} for a bad token (no auth header on
        # this endpoint). Treat presence of `c` (current price) as proof
        # the key was honoured.
        if isinstance(payload, dict) and payload.get("c") not in (None, 0):
            return ValidationResult(True, "OK")
        return ValidationResult(False, "key rejected (empty quote)")
    if r.status_code in (401, 403):
        return ValidationResult(False, "key rejected by Finnhub")
    if r.status_code == 429:
        return ValidationResult(False, "rate limit hit — try again shortly")
    return ValidationResult(False, f"HTTP {r.status_code}: {r.text[:200]}")


async def _validate_finmind(key: str) -> ValidationResult:
    """Hit FinMind's `TaiwanStockPrice` (free-tier, used by the connector
    itself) for 2330 over a 1-day window. A 200 with `data: [...]` and
    `status == 200` confirms the token is honoured. A 400 typically
    means malformed token; 401/402 means rejected; 429 is rate limit.
    """
    # 5 days back — falls within FinMind's free-tier window without
    # needing TODAY's data (which may not be ready post-market-close).
    start_date = (datetime.now(UTC).date() - timedelta(days=5)).isoformat()
    async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT) as client:
        r = await client.get(
            "https://api.finmindtrade.com/api/v4/data",
            params={
                "dataset": "TaiwanStockPrice",
                "data_id": "2330",
                "start_date": start_date,
                "token": key,
            },
        )
    if r.status_code == 200:
        try:
            payload = r.json()
        except (ValueError, json.JSONDecodeError):
            return ValidationResult(False, "non-JSON response")
        if isinstance(payload, dict) and payload.get("status") == 200:
            return ValidationResult(True, "OK")
        msg = payload.get("msg", "") if isinstance(payload, dict) else ""
        return ValidationResult(False, f"key rejected: {msg or 'unknown'}")
    if r.status_code == 400:
        return ValidationResult(False, "FinMind 400 — empty / malformed token")
    if r.status_code in (401, 402, 403):
        return ValidationResult(False, f"key rejected by FinMind (HTTP {r.status_code})")
    if r.status_code == 429:
        return ValidationResult(False, "rate limit hit — try again shortly")
    return ValidationResult(False, f"HTTP {r.status_code}: {r.text[:200]}")
