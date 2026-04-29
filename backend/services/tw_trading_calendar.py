"""TW trading-calendar helpers — data-driven, no hardcoded holiday list.

Two concerns drive this module:

  1. Should `auto_run_discussion` fire today? Weekends are easy
     (skip). Weekday national holidays (lunar new year, etc.) are
     harder — TW exchange has ~14 such days per year. Hardcoding the
     list is a maintenance tax (annual update, easy to forget); using
     `pandas.tseries.holiday` would pull a heavyweight dep. Instead
     we accept that the auto-run fires on the rare holiday weekday
     and the verifier downstream notices "no bars in the window" and
     defers — the verdict eventually lands once the next 5 *actual*
     trading days have produced bars.

  2. Computing `verify_after_date = today + 5 trading days`. Same
     simplification: walk forward 5 weekdays as a best-effort
     estimate. The verifier task itself runs daily and re-scans
     against the actual `ohlcv_daily` rows, so a holiday inside the
     window just means verification arrives a day late — the cron
     idempotency keeps that benign.

A 12-hour Redis cache memoizes the weekend check so the hot path
doesn't recompute `datetime.now(_TW)` on every scheduler tick.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

import pytz

from cache.redis_cache import cache_get, cache_set
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

_TW = pytz.timezone("Asia/Taipei")
_CACHE_KEY = "tw:trading_day:check"
_CACHE_TTL_S = 12 * 3600


def _today_tw() -> date:
    return datetime.now(_TW).date()


async def is_today_likely_trading_day(db: AsyncSession | None = None) -> bool:
    """Best-effort "should we run TW market work today" check.

    `db` is accepted but currently unused — kept in the signature so a
    future enhancement (e.g. "did `ingest_ohlcv_tw` produce rows
    yesterday? if no, this is probably a holiday tomorrow too")
    doesn't break callers.

    Cached in Redis for 12h: at the auto-run cron tick (00:00 UTC =
    08:00 Taipei), the check fires once and stays cached until the
    next day rolls over.
    """
    today = _today_tw()
    cached = await cache_get(f"{_CACHE_KEY}:{today.isoformat()}")
    if cached is not None:
        return cached == "1"

    is_weekday = today.weekday() < 5
    await cache_set(
        f"{_CACHE_KEY}:{today.isoformat()}",
        "1" if is_weekday else "0",
        ttl_seconds=_CACHE_TTL_S,
    )
    return is_weekday


def add_trading_days_estimate(start: date, n: int) -> date:
    """Walk forward `n` weekdays from `start` (exclusive) and return
    the resulting date. Used to seed `verify_after_date` after a fresh
    auto-run discussion lands; the verifier consumes it as a "don't
    grade me before this date" gate.

    A holiday landing inside the window doesn't change the return
    value — the verifier sees fewer bars, leaves verdict NULL, retries
    next day. Cron idempotency (verdict-NULL filter) handles repeat.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    cur = start
    added = 0
    while added < n:
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5:
            added += 1
    return cur


def utcnow_tw_date() -> date:
    """Convert `datetime.now(UTC)` to the TW-local date. Used to
    interpret `created_at` for the verifier — the discussion's
    "day 1" is the Taipei date the auto-run fired, not a UTC date."""
    return datetime.now(UTC).astimezone(_TW).date()


def to_tw_date(dt: datetime) -> date:
    """Helper for converting any timezone-aware datetime (e.g.
    `Discussion.created_at`) to the Taipei calendar date."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_TW).date()
