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
counts (operators read zero as "cron hasn't run yet").
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

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
) -> None:
    """Discussion's primary-market sentiment aggregate (last 48h)."""
    try:
        from services.news_sentiment_service import read_recent_market_sentiment
        ns = await read_recent_market_sentiment(
            db, market=market, limit=50, max_age_hours=48, as_of=as_of_dt,
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
            db, market="GLOBAL", limit=50, max_age_hours=48, as_of=as_of_dt,
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
) -> None:
    """Per-symbol news sentiment for each focus symbol, 7-day window.

    7-day (vs 48h for market-wide): per-symbol news is sparse — 72h
    often returned None for mid-caps with one mention every few days.
    Aggregate counts reflect the full 7-day population; `headlines`
    cap stays at 10 per symbol."""
    if not focus_symbols:
        return
    try:
        from services.news_sentiment_service import read_symbol_sentiment
        for sym in focus_symbols[:max_focus_symbols]:
            rows = await read_symbol_sentiment(
                db, market=market, symbol=sym,
                limit=10, max_age_hours=168, as_of=as_of_dt,
            )
            if rows:
                ctx["per_symbol_news_sentiment"][sym] = rows
    except Exception as exc:
        record_error("per_symbol_sentiment", exc)
