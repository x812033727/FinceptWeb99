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

# Hot-path inline scoring cap: when the archive is already covered but
# rows are unscored, score this many IDs at most per round button press.
# 100 IDs = 5 batches × 20 = ~15-30s wait given default LLM latency.
# Subsequent rounds catch up another 100, cron handles the long tail.
_HOT_PATH_INLINE_SCORE_LIMIT = 100
# Hot-path window — match the widest reader window
# (`read_symbol_sentiment` per-symbol = 7 days; `read_recent_market_sentiment`
# market-wide = 48h is a subset). `days_forward=0` because no reader
# looks past `as_of`.
_HOT_PATH_DAYS_BACK = 7
_HOT_PATH_DAYS_FORWARD = 0

# Redis lock so two concurrent rounds for the same date don't both fire
# the FinMind call. TTL covers the FinMind chunk + the inline LLM
# scoring pass (up to ~5 batches × ~2-3 s each at default cap), with
# slack for retry.
_LOCK_TTL_SECONDS = 180


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


async def _score_inserted_window(
    *, market: str, as_of: date, limit: int = 2_000,
    days_back: int = _BACKFILL_DAYS_BACK,
    days_forward: int = _BACKFILL_DAYS_FORWARD,
) -> dict[str, int]:
    """Inline-score unscored rows in the window so the discussion
    round reads SCORED rows instead of NULL-filtered nothing.

    Two callers, two configurations:

      - **Cold backfill path** (`limit=2_000`, default 30d back / 1d
        forward): we just inserted up to a few hundred new rows
        spanning the FinMind chunk; score as many as the daily
        interactive cap allows. Backfill itself took FinMind
        round-trip latency, the user is already waiting, and unscored
        rows starve the round we're about to run.

      - **Hot covered path** (`limit=100`, `days_back=7`,
        `days_forward=0`): archive already had rows, no backfill
        needed, but those rows were inserted at a time when LLM
        scoring failed silently (PR #225 empty-response bug) so
        they're still NULL. With 87K+ unscored rows in a 31-day
        window, `select_unscored_in_window`'s `published_at.desc()`
        ordering means the top 100 cluster on the latest 1-2 days —
        which fall *outside* the reader's `[as_of - 48h, as_of]`
        market-sentiment window if any of them sit in the
        `as_of + 1d` bucket. Hot path narrows the window to align
        with the readers (per-symbol 7d is the widest reader; 48h
        market-wide is a subset; never read past `as_of`) so every
        row scored is a row at least one reader can use.

    Without this step:
      1. backfill inserts (or rows already exist with) sentiment_score=NULL,
      2. read_recent_market_sentiment filters NULL rows out via
         `sentiment_score IS NOT NULL`,
      3. ctx["news_sentiment"] arrives empty,
      4. personas debate without news context — defeating the
         entire point of the auto-backfill.

    The inline scorer respects its own daily cap
    (`SENTIMENT_INTERACTIVE_BACKFILL_CAP`) so a heavy backtest day
    can't starve the cron's budget. When the cap is hit the
    remainder stays NULL and the hourly cron picks them up later.

    Returns the `score_specific_articles` result dict, or
    `{"considered": 0, ...}` when nothing was found to score.
    Errors logged but never raised — best-effort, the backfill itself
    succeeded.
    """
    from datetime import datetime as _dt

    from db.session import AsyncSessionLocal
    from services.news_sentiment_service import (
        score_specific_articles, select_unscored_in_window,
    )

    # Window bounds in UTC so the same query semantics work on
    # Postgres + SQLite test runs.
    start_dt = _dt.combine(
        as_of - timedelta(days=days_back),
        _dt.min.time(), tzinfo=UTC,
    )
    end_dt = _dt.combine(
        as_of + timedelta(days=days_forward),
        _dt.max.time(), tzinfo=UTC,
    )

    try:
        async with AsyncSessionLocal() as score_db:
            ids = await select_unscored_in_window(
                score_db, market=market,
                start=start_dt, end=end_dt,
                limit=limit,
            )
            if not ids:
                return {
                    "considered": 0, "scored": 0,
                    "batches": 0, "cap_hit": 0,
                }
            stats = await score_specific_articles(
                score_db, article_ids=ids,
            )
        log.info(
            "news_backfill.inline_scored",
            extra={
                "as_of": as_of.isoformat(),
                "considered": stats["considered"],
                "scored": stats["scored"],
                "batches": stats["batches"],
                "cap_hit": stats["cap_hit"],
            },
        )
        return stats
    except Exception as exc:
        log.warning(
            "news_backfill.inline_score_failed",
            extra={"as_of": as_of.isoformat(), "error": str(exc)},
        )
        return {
            "considered": 0, "scored": 0,
            "batches": 0, "cap_hit": 0,
            "error": str(exc),
        }


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
        # Archive is covered for this date window — but rows may still
        # be unscored. Two ways this happens in practice:
        #   1. A previous backfill inserted them while M2.7 was
        #      returning empty (PR #225 silent-empty bug, fixed by
        #      PR #225 + #226), so the inline scoring at the time
        #      wrote nothing.
        #   2. Cron's 7-day max_age window doesn't reach back to a
        #      backtest's anchor date, so historical rows are never
        #      picked up by the hourly catchup pass.
        # Either way the round needs SOME scored signal. Cap at
        # `_HOT_PATH_INLINE_SCORE_LIMIT` (5 batches × 20 ≈ 15-30s
        # wait) so the round button isn't blocked for many minutes;
        # the next round adds another batch, and cron eventually
        # catches up the long tail.
        #
        # Window is narrowed to `[as_of - 7d, as_of]` instead of the
        # backfill chunk's 30d-back-1d-forward to ALIGN with the readers
        # (`read_recent_market_sentiment` 48h, `read_symbol_sentiment`
        # 7d). With 87K+ unscored rows the desc-published_at ordering
        # would otherwise cram all 100 scored rows into the latest day,
        # often `as_of + 1d`, which falls outside *every* reader's
        # window — scored=100 but news_sentiment=null. PR #227's
        # `scored=100` symptom from production was exactly this.
        scoring_stats = await _score_inserted_window(
            market=market, as_of=as_of,
            limit=_HOT_PATH_INLINE_SCORE_LIMIT,
            days_back=_HOT_PATH_DAYS_BACK,
            days_forward=_HOT_PATH_DAYS_FORWARD,
        )
        result: dict = {
            "covered": True,
            "backfilled": 0,
            "scored": scoring_stats.get("scored", 0),
            "scoring_batches": scoring_stats.get("batches", 0),
            "scoring_cap_hit": bool(scoring_stats.get("cap_hit", 0)),
        }
        if scoring_stats.get("error"):
            # Surface the LLM failure reason so the discussion ctx
            # shows users WHY scoring returned 0 (e.g. "minimax
            # returned no content; finish_reason=length") instead of
            # an opaque scored=0 with no log access.
            result["scoring_error"] = scoring_stats["error"]
        return result

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
        # Inline-score the freshly-backfilled rows BEFORE releasing
        # the lock — this is the whole point of the auto-backfill.
        # Without this the round reads NULL-filtered nothing and
        # personas debate without news context. Bounded by
        # SENTIMENT_INTERACTIVE_BACKFILL_CAP; cap-hit means the
        # remainder waits for the hourly cron.
        scoring_stats: dict[str, int] = {
            "considered": 0, "scored": 0,
            "batches": 0, "cap_hit": 0,
        }
        if backfilled > 0:
            scoring_stats = await _score_inserted_window(
                market=market, as_of=as_of,
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
        # Inline-scoring stats so the discussion's `news_backfill`
        # diagnostic surfaces "we wrote 200 rows AND scored 100 of
        # them" — distinguishes "nothing to read" from "read but
        # cron hasn't caught up" cases the user used to hit.
        "scored": scoring_stats.get("scored", 0),
        "scoring_batches": scoring_stats.get("batches", 0),
        "scoring_cap_hit": bool(scoring_stats.get("cap_hit", 0)),
    }
    if scoring_stats.get("error"):
        result["scoring_error"] = scoring_stats["error"]
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
