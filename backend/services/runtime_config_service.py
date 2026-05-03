"""Admin-tunable env-var overrides resolved at runtime.

Each registered setting has a typed default (read from `settings.X`)
plus optional min/max bounds. Callers use `get_int(...)` / `get_float(...)`
/ `get_bool(...)` etc. — the resolver consults Redis (60 s TTL) → DB row
→ compiled-in default in that order.

Adding a new tunable:
    1. Register a `RuntimeSettingSpec` in `_REGISTRY` below.
    2. Replace the call site `settings.X` → `await runtime_config.get_int("X")`.
    3. The admin UI surfaces the new entry automatically via `list_settings()`.

The Redis layer is a soft cache, not a source of truth: a 60 s TTL means
admin edits propagate across pods within one minute without explicit
invalidation. Falls open (uses DB / settings) on Redis outage so the
read path stays available.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_cache import cache_delete, cache_get, cache_set
from config import settings
from models.runtime_setting import RuntimeSetting
from models.user import User

log = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60
_CACHE_KEY_PREFIX = "runtime_config:"

SettingType = Literal["int", "float", "bool", "str"]


@dataclass(frozen=True)
class RuntimeSettingSpec:
    key: str
    type: SettingType
    name: str          # short label shown in the admin UI
    description: str   # one-sentence "what this does"
    min_value: float | int | None = None
    max_value: float | int | None = None
    # Settings attribute name on `settings` for the compiled-in default.
    # Defaults to `key` — override only if they differ (none currently do).
    settings_attr: str = field(default="")

    def __post_init__(self) -> None:
        if not self.settings_attr:
            object.__setattr__(self, "settings_attr", self.key)


_REGISTRY: dict[str, RuntimeSettingSpec] = {
    "SENTIMENT_DAILY_LLM_CALL_CAP": RuntimeSettingSpec(
        key="SENTIMENT_DAILY_LLM_CALL_CAP",
        type="int",
        name="Sentiment daily LLM-call cap",
        description=(
            "Maximum number of LLM batches the news-sentiment scorer is "
            "allowed to run per UTC day. Each batch scores up to 20 "
            "headlines, so 100 caps the daily ceiling at ~2 000 articles. "
            "Set 0 to disable the cap entirely."
        ),
        min_value=0,
        max_value=10_000,
    ),
    "SENTIMENT_INTERACTIVE_BACKFILL_CAP": RuntimeSettingSpec(
        key="SENTIMENT_INTERACTIVE_BACKFILL_CAP",
        type="int",
        name="Sentiment interactive backfill cap",
        description=(
            "Per-day cap on inline LLM calls fired by the backtest news "
            "auto-backfill (separate from the hourly cron's cap). Each "
            "call scores up to 20 freshly-backfilled headlines so the "
            "discussion round that triggered the backfill reads scored "
            "data instead of NULL-filtered nothing. 100 = ~2 000 "
            "articles/day = ~10-40 backtests fully scored inline. Set 0 "
            "to disable inline scoring (rounds will read empty news)."
        ),
        min_value=0,
        max_value=10_000,
    ),
    "DISCUSSION_PERSONA_TIMEOUT_SECONDS": RuntimeSettingSpec(
        key="DISCUSSION_PERSONA_TIMEOUT_SECONDS",
        type="int",
        name="Discussion persona timeout (s)",
        description=(
            "How long a single persona's LLM turn may run before it's "
            "aborted and a placeholder turn is persisted. Lower values "
            "abandon stuck providers faster; raise if you use very slow "
            "models."
        ),
        min_value=10,
        max_value=600,
    ),
    "AI_REQUESTS_VIEWER_DAILY": RuntimeSettingSpec(
        key="AI_REQUESTS_VIEWER_DAILY",
        type="int",
        name="Viewer daily AI request quota",
        description=(
            "Number of /api/ai/chat requests a viewer-role user can make "
            "per UTC day. Discussion rounds count len(persona_ids) "
            "requests; conclude counts 1."
        ),
        min_value=0,
        max_value=10_000,
    ),
    "AI_REQUESTS_ANALYST_DAILY": RuntimeSettingSpec(
        key="AI_REQUESTS_ANALYST_DAILY",
        type="int",
        name="Analyst daily AI request quota",
        description="Same as viewer quota but for analyst / admin roles.",
        min_value=0,
        max_value=100_000,
    ),
    "FINMIND_HOURLY_REQUEST_LIMIT": RuntimeSettingSpec(
        key="FINMIND_HOURLY_REQUEST_LIMIT",
        type="int",
        name="FinMind hourly request budget",
        description=(
            "Conservative cap on FinMind API calls per rolling hour. "
            "FinMind's registered free tier is 600/hour (300/hour "
            "anonymous); we default to 550 to leave headroom for "
            "incident retries."
        ),
        min_value=0,
        max_value=10_000,
    ),
}


@dataclass
class SettingInfo:
    key: str
    type: SettingType
    name: str
    description: str
    min_value: float | int | None
    max_value: float | int | None
    default_value: Any
    effective_value: Any
    is_overridden: bool
    updated_at: datetime | None
    updated_by_email: str | None


def known_keys() -> list[str]:
    return list(_REGISTRY.keys())


def get_spec(key: str) -> RuntimeSettingSpec:
    spec = _REGISTRY.get(key)
    if spec is None:
        raise ValueError(f"unknown runtime setting: {key!r}")
    return spec


# ── Type coercion + validation ──────────────────────────────────────


def _coerce(spec: RuntimeSettingSpec, raw: Any) -> Any:
    """Coerce `raw` into the type the spec demands. Raises ValueError on
    bad input or out-of-bounds values."""
    if spec.type == "int":
        try:
            v = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{spec.key} must be int (got {raw!r})") from exc
        if spec.min_value is not None and v < spec.min_value:
            raise ValueError(f"{spec.key} must be >= {spec.min_value}")
        if spec.max_value is not None and v > spec.max_value:
            raise ValueError(f"{spec.key} must be <= {spec.max_value}")
        return v
    if spec.type == "float":
        try:
            v = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{spec.key} must be float (got {raw!r})") from exc
        if spec.min_value is not None and v < spec.min_value:
            raise ValueError(f"{spec.key} must be >= {spec.min_value}")
        if spec.max_value is not None and v > spec.max_value:
            raise ValueError(f"{spec.key} must be <= {spec.max_value}")
        return v
    if spec.type == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.lower() in ("true", "false", "1", "0", "yes", "no"):
            return raw.lower() in ("true", "1", "yes")
        raise ValueError(f"{spec.key} must be bool (got {raw!r})")
    # str
    return str(raw)


def _default(spec: RuntimeSettingSpec) -> Any:
    """Return the compiled-in default (i.e. what `.env` / `Settings()`
    resolved at import time)."""
    return getattr(settings, spec.settings_attr)


# ── Resolver ────────────────────────────────────────────────────────


async def _read_db(db: AsyncSession, key: str) -> Any | None:
    row = await db.get(RuntimeSetting, key)
    return row.value if row is not None else None


async def get(db: AsyncSession, key: str) -> Any:
    """Resolve `key`'s effective value: cache → DB → compiled default.

    Caches the resolved value in Redis for `_CACHE_TTL_SECONDS` so the
    next per-request read is O(1). On Redis outage we silently bypass
    the cache and hit DB / settings each time — slower but never broken.
    """
    spec = get_spec(key)
    cache_key = _CACHE_KEY_PREFIX + key

    try:
        cached = await cache_get(cache_key)
    except Exception:
        cached = None
    if cached is not None:
        try:
            return _coerce(spec, json.loads(cached))
        except (ValueError, json.JSONDecodeError):
            # Cached value got corrupted (rare); fall through to DB read
            # and overwrite below.
            pass

    db_val = await _read_db(db, key)
    val = _coerce(spec, db_val) if db_val is not None else _default(spec)

    try:
        await cache_set(cache_key, json.dumps(val), _CACHE_TTL_SECONDS)
    except Exception:
        pass
    return val


async def get_int(db: AsyncSession, key: str) -> int:
    return int(await get(db, key))


async def get_float(db: AsyncSession, key: str) -> float:
    return float(await get(db, key))


async def get_bool(db: AsyncSession, key: str) -> bool:
    return bool(await get(db, key))


async def get_str(db: AsyncSession, key: str) -> str:
    return str(await get(db, key))


# ── Admin CRUD ──────────────────────────────────────────────────────


async def list_settings(db: AsyncSession) -> list[SettingInfo]:
    """Return every registered setting with its compiled default + the
    currently effective value + audit info (who set it, when).
    Outer-joins on users so a deleted setter still shows the timestamp.
    """
    stmt = (
        select(RuntimeSetting, User.email)
        .outerjoin(User, RuntimeSetting.updated_by_id == User.id)
    )
    rows = list((await db.execute(stmt)).all())
    by_key: dict[str, tuple[RuntimeSetting, str | None]] = {
        r[0].key: (r[0], r[1]) for r in rows
    }

    out: list[SettingInfo] = []
    for key, spec in _REGISTRY.items():
        entry = by_key.get(key)
        ov, email = (entry[0], entry[1]) if entry else (None, None)
        default_val = _default(spec)
        try:
            effective = _coerce(spec, ov.value) if ov is not None else default_val
        except ValueError:
            # Persisted value drifted out of bounds (e.g. validation
            # tightened in code). Surface the default + we'll let the
            # admin re-save to repair.
            effective = default_val
        out.append(SettingInfo(
            key=key,
            type=spec.type,
            name=spec.name,
            description=spec.description,
            min_value=spec.min_value,
            max_value=spec.max_value,
            default_value=default_val,
            effective_value=effective,
            is_overridden=ov is not None,
            updated_at=ov.updated_at if ov else None,
            updated_by_email=email,
        ))
    return out


async def upsert(
    db: AsyncSession,
    key: str,
    raw_value: Any,
    *,
    updated_by_id: uuid.UUID,
) -> SettingInfo:
    spec = get_spec(key)
    value = _coerce(spec, raw_value)   # raises ValueError on type / bounds

    row = await db.get(RuntimeSetting, key)
    if row is None:
        row = RuntimeSetting(key=key, value=value, updated_by_id=updated_by_id)
        db.add(row)
    else:
        row.value = value
        row.updated_by_id = updated_by_id
    await db.commit()
    await db.refresh(row)

    # Invalidate the Redis cache so the next read sees the new value
    # (instead of waiting up to 60 s for natural TTL expiry).
    try:
        await cache_delete(_CACHE_KEY_PREFIX + key)
    except Exception as exc:
        log.warning("runtime_config.cache_invalidate_failed",
                    extra={"key": key, "error": str(exc)})

    email = await db.scalar(select(User.email).where(User.id == updated_by_id))
    return SettingInfo(
        key=key,
        type=spec.type,
        name=spec.name,
        description=spec.description,
        min_value=spec.min_value,
        max_value=spec.max_value,
        default_value=_default(spec),
        effective_value=value,
        is_overridden=True,
        updated_at=row.updated_at,
        updated_by_email=email,
    )


async def delete_override(db: AsyncSession, key: str) -> None:
    """Revert to the compiled-in default. No-op if no override exists."""
    get_spec(key)   # validation — raises on unknown
    row = await db.get(RuntimeSetting, key)
    if row is not None:
        await db.delete(row)
        await db.commit()
    try:
        await cache_delete(_CACHE_KEY_PREFIX + key)
    except Exception:
        pass
