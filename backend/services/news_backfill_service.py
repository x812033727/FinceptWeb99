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


# Per-symbol fan-out cap when market-wide is paywalled (PR #218).
# Discussions typically have 1-3 focus symbols; capping at 5 limits
# auto-backfill to a few seconds + a few FinMind calls even on
# pathologically long focus_symbols lists.
_PER_SYMBOL_CAP = 5
# Tiny pacing between per-symbol calls so we don't trip rate limits
# on a tier where TaiwanStockNews per-symbol IS allowed but at a
# limited concurrency. Sequential awaits + 0.3s pause = polite.
_PER_SYMBOL_DELAY_S = 0.3


def _format_finmind_error(exc: BaseException) -> str:
    """Translate a FinMind exception into the most informative error
    string we can surface back to the user. Three cases:

      1. Paywall body present → call out tier mismatch explicitly.
      2. Other JSON body present → echo the upstream `msg`.
      3. No JSON body → fall through to httpx default, with the
         token query param redacted (the URL leaks token=<jwt>).
    """
    from data.tw.finmind_paywall import (
        extract_body_message, looks_like_paywall,
    )
    body_msg = extract_body_message(exc)
    if body_msg and looks_like_paywall(body_msg):
        return (
            f"FinMind paywall — TaiwanStockNews requires a paid "
            f"sponsor tier. Upstream says: {body_msg}"
        )
    if body_msg:
        return f"FinMind error: {body_msg}"
    return _sanitise_token(str(exc))


async def ensure_news_archive_covers(
    db: AsyncSession, *, market: str, as_of: date,
    focus_symbols: list[str] | None = None,
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

    Two backfill strategies tried in order:

      1. **Market-wide** (`data_id=""`) — one FinMind call returns
         every listed company's news for the date window. Fast +
         compact, but typically requires a paid sponsor tier above
         "Sponsor".

      2. **Per-symbol fan-out** (PR #218 + #219) — fired automatically
         on either:
           a. strategy 1 raises a FinMind paywall response (HTTP 400
              + tier-mismatch body), OR
           b. strategy 1 succeeds but returns 0 rows AND
              `focus_symbols` is non-empty (PR #219 — Sponsor tier
              sometimes responds with HTTP 200 + status=400 in the
              body for restricted datasets, which `_query` treats as
              an empty list rather than a paywall).
         Iterates the topic's focus symbols (capped at 5) calling
         `data_id=<sym>`. The Sponsor tier allows per-symbol queries,
         so this gets news flowing for the discussion's mentioned
         tickers even when market-wide is denied. No fallback when
         there are no focus symbols — the discussion would have no
         use for the arbitrary news.

    Result keys:
      - `covered`: bool
      - `backfilled`: int (rows handed to insert_news_articles)
      - `error`: str (when both strategies failed)
      - `fallback`: "per-symbol" (when strategy 2 succeeded)
      - `skipped`: "lock" | "non-tw" (early skip reason)

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
        log.info("news_backfill.skipped_lock_held",
                 extra={"as_of": as_of.isoformat()})
        return {"covered": False, "backfilled": 0, "skipped": "lock"}

    backfilled = 0
    error_str: str | None = None
    fallback_used: str | None = None

    try:
        try:
            backfilled = await _do_backfill(market=market, as_of=as_of)
        except Exception as exc:
            from data.tw.finmind_paywall import (
                extract_body_message, looks_like_paywall,
            )
            is_paywall = looks_like_paywall(extract_body_message(exc))

            if is_paywall and focus_symbols:
                # Sponsor tier blocks `data_id=""` market-wide calls
                # with HTTP 400 + paywall body. Fall back to fan-out.
                log.info(
                    "news_backfill.paywall_falling_back_to_per_symbol",
                    extra={
                        "as_of": as_of.isoformat(),
                        "symbols": list(focus_symbols[:_PER_SYMBOL_CAP]),
                    },
                )
                try:
                    backfilled = await _do_backfill_per_symbol(
                        market=market, as_of=as_of,
                        symbols=list(focus_symbols[:_PER_SYMBOL_CAP]),
                    )
                    fallback_used = "per-symbol"
                except Exception as exc2:
                    error_str = _format_finmind_error(exc2)
                    log.warning(
                        "news_backfill.per_symbol_fallback_failed",
                        extra={
                            "as_of": as_of.isoformat(),
                            "error": error_str,
                        },
                    )
            else:
                error_str = _format_finmind_error(exc)
                log.warning(
                    "news_backfill.failed",
                    extra={"as_of": as_of.isoformat(), "error": error_str},
                )
        else:
            # Market-wide didn't raise — but it might've silently
            # returned 0 rows. Sponsor tier sometimes responds with
            # HTTP 200 + status=400 in the JSON body for restricted
            # datasets, which `_query` treats as empty. Without this
            # branch the fallback only fires when FinMind explicitly
            # raises, missing the silent-deny case (PR #219).
            #
            # Trying per-symbol when market-wide returned 0 is also
            # harmless when it's a genuine "no news for this date" —
            # per-symbol returns 0 too, no extra rows written.
            if backfilled == 0 and focus_symbols:
                log.info(
                    "news_backfill.empty_market_wide_falling_back_to_per_symbol",
                    extra={
                        "as_of": as_of.isoformat(),
                        "symbols": list(focus_symbols[:_PER_SYMBOL_CAP]),
                    },
                )
                try:
                    backfilled = await _do_backfill_per_symbol(
                        market=market, as_of=as_of,
                        symbols=list(focus_symbols[:_PER_SYMBOL_CAP]),
                    )
                    if backfilled > 0:
                        fallback_used = "per-symbol"
                except Exception as exc:
                    error_str = _format_finmind_error(exc)
                    log.warning(
                        "news_backfill.per_symbol_fallback_failed",
                        extra={
                            "as_of": as_of.isoformat(),
                            "error": error_str,
                        },
                    )
    finally:
        await release_lock(lock_key)

    # Re-count so the caller knows whether we crossed the threshold.
    try:
        new_count = await _count_recent_articles(
            db, market=market, end=as_of,
        )
    except Exception:
        new_count = existing + backfilled

    result: dict = {
        "covered": new_count >= _MIN_ARTICLES_THRESHOLD,
        "backfilled": backfilled,
    }
    if error_str:
        result["error"] = error_str
    if fallback_used:
        result["fallback"] = fallback_used
    return result


async def _do_backfill_per_symbol(
    *, market: str, as_of: date, symbols: list[str],
) -> int:
    """Per-symbol fan-out fallback for Sponsor-tier paywall.

    Sponsor tokens can't issue `data_id=""` market-wide
    TaiwanStockNews queries (they 400 with a paywall body) but CAN
    issue per-symbol queries — same dataset, same window, just one
    `data_id=<sym>` at a time.

    Sequential awaits with a 0.3s pause between symbols. With
    `_PER_SYMBOL_CAP=5`, the worst case is 5 × ~0.5s = ~2.5s
    additional latency on top of the failed market-wide attempt.
    Per-symbol failures are logged and skipped — one symbol's outage
    doesn't blank the others.

    Returns the count of rows handed to `insert_news_articles`
    aggregated across all symbols (actual DB inserts smaller after
    sha256 dedup).
    """
    import asyncio

    import data.tw.finmind_connector as finmind
    from db.session import AsyncSessionLocal
    from scripts.backfill_news_finmind import _to_row
    from services.ingest.repository import insert_news_articles

    chunk_start = as_of - timedelta(days=_BACKFILL_DAYS_BACK)
    chunk_end = as_of + timedelta(days=_BACKFILL_DAYS_FORWARD)

    all_rows = []
    last_exc: Exception | None = None
    for i, sym in enumerate(symbols):
        if i > 0:
            await asyncio.sleep(_PER_SYMBOL_DELAY_S)
        try:
            items = await finmind.get_news(
                chunk_start.isoformat(),
                symbol=sym,
                end_date=chunk_end.isoformat(),
            )
        except Exception as exc:
            last_exc = exc
            log.warning(
                "news_backfill.per_symbol_call_failed",
                extra={"symbol": sym, "error": str(exc)},
            )
            continue
        for item in items:
            row = _to_row(item, market)
            if row is not None:
                all_rows.append(row)

    if not all_rows:
        # Every symbol either failed or returned nothing. Re-raise
        # the last exception (if any) so the caller can surface the
        # FinMind error; if every call genuinely returned [], silent
        # zero is fine.
        if last_exc is not None:
            raise last_exc
        return 0

    async with AsyncSessionLocal() as write_db:
        await insert_news_articles(write_db, all_rows)
    log.info(
        "news_backfill.per_symbol_inserted",
        extra={
            "as_of": as_of.isoformat(),
            "rows": len(all_rows),
            "symbols": len(symbols),
        },
    )
    return len(all_rows)


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
