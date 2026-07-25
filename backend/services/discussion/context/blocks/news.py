"""News sentiment context blocks (DB-bound).

Three sub-blocks driven by the same reader (`read_recent_market_sentiment`
/ `read_symbol_sentiment`):

  - `news_sentiment` — discussion's market-wide aggregate (TW / US)
  - `international_sentiment` — global / Fed / FOMC translated zh-TW.
    Fetched regardless of `market` because rates / global macro is
    relevant to TW personas just as much as US ones.
  - `per_symbol_news_sentiment` — only when `focus_symbols` supplied.

Backtest semantics: in backtest mode (`as_of` set) the news archive
may not reach back to the anchor date — the deploy-day ingest
window is finite. When `as_of` is set AND `headlines` is empty, the
block is dropped to `None` so the LLM reads "we have no data here"
instead of "market has no sentiment". Live mode preserves the empty
counts (operators read zero as "cron hasn't run yet"). Backtests do
not try to backfill their way out of that gap — see
`fetch_market_sentiment` for why backfilling cannot help.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from db.session import AsyncSessionLocal

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

ErrorRecorder = Callable[[str, Exception], None]


async def fetch_market_sentiment(
    ctx: dict[str, Any],
    db: "AsyncSession",
    *,
    market: str,
    as_of_dt: datetime | None,
    record_error: ErrorRecorder,
    focus_symbols: list[str] | None = None,
) -> None:
    """Discussion's primary-market sentiment aggregate (last 48h).

    No auto-backfill in backtest mode. PRs #214-#227 called
    `ensure_news_archive_covers` here whenever `as_of_dt` was set, to
    insert the anchor's 14-day news window and inline-score it. That
    work cannot affect what this function returns:

      - inserted rows land with `sentiment_score = NULL`, and the
        reader filters `sentiment_score IS NOT NULL`;
      - `score_specific_articles` stamps `sentiment_scored_at = now()`,
        and in backtest mode the reader filters
        `sentiment_scored_at <= anchor` (the correct look-ahead guard —
        a persona at `as_of` could not have seen a score written
        today). `now()` is always > a past anchor, so a row scored
        during a backtest is invisible to that backtest, forever.

    So the ctx this builds is byte-identical with or without the call:
    either `None`, or the pre-existing rows the hourly cron scored
    before the anchor. What the call did cost was real — measured on a
    60-session replay, ~35 min and ~90K unscored rows per anchor date
    before the first persona spoke, plus the LLM spend on scores no
    backtest can read.

    `news_sentiment` therefore stays honestly empty in backtest mode
    and `_record_data_gaps` names it as a gap, which is the truthful
    answer: the archive was not scored at that point in time.

    If the scorer is ever changed to backdate `sentiment_scored_at`
    (e.g. to `published_at`), revisit this — backfilling would then
    genuinely change what a backtest reads.
    """
    _ = focus_symbols  # kept for signature compat with the builder
    try:
        from services.news_sentiment_service import read_recent_market_sentiment
        ns = await read_recent_market_sentiment(
            db, market=market, limit=30, max_age_hours=48, as_of=as_of_dt,
        )
        if as_of_dt is not None and not (ns or {}).get("headlines"):
            ctx["news_sentiment"] = None
        else:
            ctx["news_sentiment"] = ns
    except Exception as exc:
        record_error("news_sentiment", exc)


async def fetch_international_sentiment(
    ctx: dict[str, Any],
    db: "AsyncSession",
    *,
    as_of_dt: datetime | None,
    record_error: ErrorRecorder,
) -> None:
    """Global / Fed / FOMC sentiment block — same reader, market='GLOBAL'."""
    try:
        from services.news_sentiment_service import read_recent_market_sentiment
        intl = await read_recent_market_sentiment(
            db, market="GLOBAL", limit=30, max_age_hours=48, as_of=as_of_dt,
        )
        if as_of_dt is not None and not (intl or {}).get("headlines"):
            ctx["international_sentiment"] = None
        else:
            ctx["international_sentiment"] = intl
    except Exception as exc:
        record_error("international_sentiment", exc)


async def fetch_per_symbol_sentiment(
    ctx: dict[str, Any],
    db: "AsyncSession",
    *,
    market: str,
    focus_symbols: list[str] | None,
    as_of_dt: datetime | None,
    record_error: ErrorRecorder,
    max_focus_symbols: int,
    progress_cb: Callable[..., Any] | None = None,
) -> None:
    """Per-symbol news sentiment for each focus symbol, 7-day window.

    7-day (vs 48h for market-wide): per-symbol news is sparse — 72h
    often returned None for mid-caps with one mention every few days.
    Aggregate counts reflect the full 7-day population; `headlines`
    cap stays at 10 per symbol.

    Per-symbol reads fan out via `asyncio.gather` (C2-2 from
    `misty-mixing-harbor.md`) so a 5-symbol focus list trims this
    block's wall time from ~5 × DB query to ~max(DB queries). Each
    coroutine opens its own `AsyncSessionLocal` since SQLAlchemy
    `AsyncSession` is not safe to share across concurrent awaits —
    the passed `db` is kept for signature compat with the caller
    (existing tests in `test_discussion_context_blocks.py`) but
    intentionally unused.

    `progress_cb` (C1-3) is called as each symbol's read completes
    with the cumulative `done / total` so the SSE preparing card
    can render `"Scoring news sentiment 3/5"` instead of waiting
    silently for the full window. Stage label is held at
    `scoring_news_sentiment` (the builder's parent milestone) so an
    older frontend that ignores `done` / `total` still sees the
    same stage transition it always did.
    """
    if not focus_symbols:
        return
    from services.news_sentiment_service import read_symbol_sentiment

    targets = focus_symbols[:max_focus_symbols]
    total = len(targets)
    # Mutable counter shared by all coroutines. asyncio is single-
    # threaded so `done[0] += 1` is atomic at the bytecode level;
    # no lock needed.
    done = [0]

    async def _one(sym: str) -> tuple[str, Any]:
        try:
            async with AsyncSessionLocal() as own_db:
                result: Any = await read_symbol_sentiment(
                    own_db, market=market, symbol=sym,
                    limit=5, max_age_hours=168, as_of=as_of_dt,
                )
        except Exception:
            result = None
        done[0] += 1
        if progress_cb is not None:
            try:
                await progress_cb(
                    "scoring_news_sentiment", done=done[0], total=total,
                )
            except Exception:
                # Caller's SSE pipe being broken shouldn't blank the
                # ctx — the round still has useful data even without
                # the progress event.
                pass
        return sym, result

    try:
        results = await asyncio.gather(*(_one(s) for s in targets))
    except Exception as exc:
        record_error("per_symbol_sentiment", exc)
        return
    for sym, rows in results:
        if rows:
            ctx["per_symbol_news_sentiment"][sym] = rows
