"""API-key issuance + verification.

Mirrors GitHub PAT / Stripe key shape:
  - User sees `fck_live_<random32>` once at creation
  - We persist `prefix` (first 12 chars: `fck_live_<8>`) + sha256 of
    the full key
  - Lookup at request time: WHERE prefix = ? then hash-compare

The two-column scheme means an attacker who exfiltrates `api_keys`
can't replay any key without also knowing the suffix; and the
indexed `prefix` column keeps lookup O(1) without a full-table
hash scan.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from finmind.models.billing import ApiKey

KEY_PREFIX = "fck_live_"  # "fincept clone, live key"
PREFIX_LEN = len(KEY_PREFIX) + 8  # KEY_PREFIX + 8 random chars
SUFFIX_LEN = 24


def _hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


@dataclass
class IssuedKey:
    plaintext: str
    prefix: str
    record_id: int


async def issue_key(
    session: AsyncSession,
    *,
    owner_email: str,
    subscription_id: int | None = None,
    name: str | None = None,
    scope_datasets: list[str] | None = None,
) -> IssuedKey:
    """Generate a fresh key, persist its hash + prefix, return the
    plaintext to be shown to the user once.

    The plaintext is NEVER stored — caller is responsible for
    surfacing it to the user immediately and discarding it after.
    """
    full_random = secrets.token_urlsafe(PREFIX_LEN + SUFFIX_LEN)[: PREFIX_LEN + SUFFIX_LEN]
    plaintext = f"{KEY_PREFIX}{full_random}"
    prefix = plaintext[:PREFIX_LEN]
    key_hash = _hash_key(plaintext)

    record = ApiKey(
        prefix=prefix,
        key_hash=key_hash,
        owner_email=owner_email,
        subscription_id=subscription_id,
        name=name,
        scope_datasets=scope_datasets,
        enabled=True,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)

    return IssuedKey(plaintext=plaintext, prefix=prefix, record_id=record.id)


async def verify_key(
    session: AsyncSession, plaintext: str
) -> ApiKey | None:
    """Resolve plaintext key → ApiKey row, or None if invalid /
    disabled / expired.

    Three-step:
      1. Prefix match (indexed, O(1))
      2. sha256 compare (constant-time via `hmac.compare_digest`)
      3. Status check (enabled + not expired)
    """
    if not plaintext or not plaintext.startswith(KEY_PREFIX):
        return None

    prefix = plaintext[:PREFIX_LEN]
    candidates = (
        await session.execute(
            select(ApiKey).where(ApiKey.prefix == prefix)
        )
    ).scalars().all()
    if not candidates:
        return None

    expected_hash = _hash_key(plaintext)
    import hmac

    for row in candidates:
        if not hmac.compare_digest(row.key_hash, expected_hash):
            continue
        if not row.enabled:
            return None
        if row.expires_at is not None:
            now = datetime.now(tz=timezone.utc)
            if row.expires_at.tzinfo is None:
                exp = row.expires_at.replace(tzinfo=timezone.utc)
            else:
                exp = row.expires_at
            if exp < now:
                return None
        return row

    return None


async def touch_last_used(
    session: AsyncSession, key_id: int
) -> None:
    """Bump `last_used_at` to NOW(). Best-effort — failure here
    must not break the request, so callers should swallow exceptions."""
    await session.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id)
        .values(last_used_at=datetime.now(tz=timezone.utc))
    )
    await session.commit()
