"""Auto-backfill news archive for backtest discussions.

When a discussion runs in backtest mode (`as_of` set to a past date),
the news_sentiment block needs articles published before that date.
The hourly Google News RSS ingest only reaches back ~14 days, so older
backtest dates surface the "archive doesn't reach this date" warning
unless an admin manually ran `scripts/backfill_news_finmind.py`.

This service is the auto-trigger version: when a backtest round runs
and finds the archive sparse for the relevant window, it kicks off a
narrow FinMind backfill (just the 30-day window around `as_of`) so
personas see real contemporaneous news without manual intervention.

Cheap on hot path: one COUNT query (~ms). Backfill only fires when the
archive is genuinely empty for the window, and is idempotent via the
existing sha256(title+link) dedup at the insert layer.

Failure modes:
  - No paid FINMIND_TOKEN → FinMind returns paywall response, backfill
    yields zero rows → degrades silently to the existing warning.
  - FinMind transient 5xx → same — best-effort, never blocks the
    discussion.
  - Concurrent rounds for the same date → Redis lock dedups, only one
    fires the FinMind call.

Bounded by `_WINDOW_DAYS_BACK` so the auto-trigger doesn't accidentally
pull years of data on a single discussion start. Admins backfilling the
full archive should still use the CLI (`scripts/backfill_news_finmind`).
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_cache import acquire_lock, release_lock
from models.news_article import NewsArticle

log = logging.getLogger(__name__)

# Look at the 14-day window ENDING at `as_of` — that's the slice the
# discussion's `read_recent_market_sentiment(max_age_hours=48)` and
# `read_symbol_sentiment(max_age_hours=168)` care about.
_WINDOW_DAYS_BACK = 14
# Below this many articles in the window, treat as "uncovered".
_MIN_ARTICLES_THRESHOLD = 5
# Backfill chunk: pull a slightly wider window than the threshold check
# so personas with extended `max_age_hours` (per-symbol, 7-day) also
# see results, and so adjacent backtest dates reuse the same data.
_BACKFILL_DAYS_BACK = 30
_BACKFILL_DAYS_FORWARD = 1  # capture publish-date-after-event articles

# Redis lock so two concurrent rounds for the same date don't both fire
# the FinMind call. TTL covers a slow chunk + reasonable retry slack.
_LOCK_TTL_SECONDS = 60


def _lock_key(market: str, as_of: date) -> str:
    return f"lock:news_backfill:{market}:{as_of.isoformat()}"


def _sanitise_token(s: str) -> str:
    """Strip any `token=<jwt>` query param from the string. httpx's
    default 4xx error message includes the full request URL — and the
    URL has the FinMind token as a query string. Without sanitisation,
    that token leaks into `ctx["news_backfill"]["error"]` (which the
    user sees in the discussion JSON, browser DevTools, etc).
    Conservative regex: strips the value only, preserves surrounding
    URL structure for diagnostics."""
    import re
    return re.sub(r"token=[^&\s'\"]+", "token=<redacted>", s)


async def _count_recent_articles(
    db: AsyncSession, *, market: str, end: date,
) -> int:
    """Count news_articles in `(end - 14 days, end)`. Used as the
    coverage probe before deciding whether to fire a backfill."""
    start_dt = datetime.combine(
        end - timedelta(days=_WINDOW_DAYS_BACK),
        datetime.min.time(), tzinfo=UTC,
    )
    end_dt = datetime.combine(end, datetime.max.time(), tzinfo=UTC)
    stmt = (
        select(func.count(NewsArticle.id))
        .where(
            NewsArticle.market == market,
            NewsArticle.published_at >= start_dt,
            NewsArticle.published_at <= end_dt,
        )
    )
    return int(await db.scalar(stmt) or 0)


async def ensure_news_archive_covers(
    db: AsyncSession, *, market: str, as_of: date,
) -> dict:
    """Best-effort: ensure `news_articles` has at least
    `_MIN_ARTICLES_THRESHOLD` rows in the 14-day window ending at
    `as_of`. If not, kick off a FinMind backfill for that window.

    Hot path (archive already populated): one COUNT, returns
    `{covered: True, backfilled: 0}` in ~ms.

    Cold path: acquires a Redis lock, calls FinMind, inserts rows,
    returns `{covered: True/False, backfilled: N, error: str?}`.
    Never raises — backfill failures degrade silently to the existing
    "archive doesn't reach this date" warning that the discussion
    UI already renders.

    `market` only `"TW"` is supported today (FinMind dataset is
    Taiwan-specific). Caller should skip non-TW markets.
    """
    if market != "TW":
        return {"covered": False, "backfilled": 0, "skipped": "non-tw"}

    try:
        existing = await _count_recent_articles(db, market=market, end=as_of)
    except Exception as exc:
        log.warning(
            "news_backfill.count_failed",
            extra={"as_of": as_of.isoformat(), "error": str(exc)},
        )
        return {"covered": False, "backfilled": 0, "error": f"count: {exc}"}

    if existing >= _MIN_ARTICLES_THRESHOLD:
        return {"covered": True, "backfilled": 0}

    lock_key = _lock_key(market, as_of)
    if not await acquire_lock(lock_key, _LOCK_TTL_SECONDS):
        # Another concurrent round is already backfilling this date —
        # skip. The eventual DB row count will be correct for whichever
        # round reads after the winner finishes.
        log.info("news_backfill.skipped_lock_held",
                 extra={"as_of": as_of.isoformat()})
        return {"covered": False, "backfilled": 0, "skipped": "lock"}

    try:
        backfilled = await _do_backfill(market=market, as_of=as_of)
    except Exception as exc:
        # FinMind tier-mismatch / dataset-paywall errors come through
        # as HTTP 400 with a JSON body that says e.g. "Your level is
        # register. Please update your user level." httpx's default
        # error string only shows "Client error '400 Bad Request'" +
        # the URL, which is useless for diagnosis (and also leaks the
        # token in the URL). Pull the upstream body message out via
        # the shared paywall detector + surface it as the error.
        from data.tw.finmind_paywall import (
            extract_body_message, looks_like_paywall,
        )
        body_msg = extract_body_message(exc)
        if body_msg and looks_like_paywall(body_msg):
            error_str = (
                f"FinMind paywall — TaiwanStockNews requires a paid "
                f"sponsor tier. Upstream says: {body_msg}"
            )
        elif body_msg:
            error_str = f"FinMind error: {body_msg}"
        else:
            # No JSON body parseable from the response — fall back to
            # httpx's default message (sanitised to drop the token).
            error_str = _sanitise_token(str(exc))
        log.warning(
            "news_backfill.failed",
            extra={"as_of": as_of.isoformat(), "error": error_str},
        )
        return {"covered": False, "backfilled": 0, "error": error_str}
    finally:
        await release_lock(lock_key)

    # Re-count so the caller knows whether we crossed the threshold.
    try:
        new_count = await _count_recent_articles(
            db, market=market, end=as_of,
        )
    except Exception:
        new_count = existing + backfilled

    return {
        "covered": new_count >= _MIN_ARTICLES_THRESHOLD,
        "backfilled": backfilled,
    }


async def _do_backfill(*, market: str, as_of: date) -> int:
    """Pull a single FinMind chunk for the relevant window + insert.
    Returns count of rows handed to insert_news_articles (actual
    inserts smaller after sha256 dedup against existing rows)."""
    import data.tw.finmind_connector as finmind
    from db.session import AsyncSessionLocal
    from scripts.backfill_news_finmind import _to_row
    from services.ingest.repository import insert_news_articles

    chunk_start = as_of - timedelta(days=_BACKFILL_DAYS_BACK)
    chunk_end = as_of + timedelta(days=_BACKFILL_DAYS_FORWARD)
    items = await finmind.get_news(
        chunk_start.isoformat(),
        symbol="",
        end_date=chunk_end.isoformat(),
    )
    if not items:
        log.info(
            "news_backfill.empty_response",
            extra={
                "as_of": as_of.isoformat(),
                "reason": "FinMind returned no rows — likely paywalled "
                          "or no news in window",
            },
        )
        return 0

    rows = []
    for item in items:
        row = _to_row(item, market)
        if row is not None:
            rows.append(row)

    if not rows:
        return 0

    async with AsyncSessionLocal() as write_db:
        await insert_news_articles(write_db, rows)
    log.info(
        "news_backfill.inserted",
        extra={"as_of": as_of.isoformat(), "rows": len(rows)},
    )
    return len(rows)
