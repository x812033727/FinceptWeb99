"""TW chip-metric + fundamentals context blocks (DB-bound).

These read from `tw_institutional_daily`, `tw_margin_daily`,
`tw_revenue_monthly`, `tw_buybacks`, `tw_govt_bank_flow_daily`, and
`tw_market_institutional_daily`. Daily ingest crons populate them;
empty result (cron hasn't run yet, fresh deploy) is the expected
no-signal state — personas just won't reference these.

TW-only — non-TW markets skip the whole module so TWSE-specific data
doesn't bleed into US discussions.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

ErrorRecorder = Callable[[str, Exception], None]


async def fetch_top_foreign_buyers(
    ctx: dict[str, Any],
    db: "AsyncSession",
    *,
    as_of: date | None,
    record_error: ErrorRecorder,
) -> None:
    """Ranked net foreign buy over the last 5 trading days. Personas
    cite "外資連 5 日買超 N 億" without burning an LLM tool call."""
    try:
        from services.discussion_service import _tag_industry
        from services.ingest.repository import read_top_foreign_buyers
        ctx["top_foreign_buyers"] = _tag_industry(
            await read_top_foreign_buyers(
                db, market="TW", days=5, limit=10, as_of=as_of,
            )
        )
    except Exception as exc:
        record_error("top_foreign_buyers", exc)


async def fetch_margin_balance_trend(
    ctx: dict[str, Any],
    db: "AsyncSession",
    *,
    as_of: date | None,
    record_error: ErrorRecorder,
) -> None:
    """Latest market-wide margin + short balance — leverage / retail-
    activity proxy ("散戶融資餘額連續 X 日創高")."""
    try:
        from services.ingest.repository import read_market_margin_balance_trend
        ctx["margin_balance_trend"] = await read_market_margin_balance_trend(
            db, market="TW", days=5, as_of=as_of,
        )
    except Exception as exc:
        record_error("margin_balance_trend", exc)


async def fetch_top_revenue_growers(
    ctx: dict[str, Any],
    db: "AsyncSession",
    *,
    as_of: date | None,
    record_error: ErrorRecorder,
) -> None:
    """Top YoY revenue growth in the latest reported month."""
    try:
        from services.discussion_service import _tag_industry
        from services.ingest.repository import read_top_revenue_growers
        ctx["top_revenue_growers"] = _tag_industry(
            await read_top_revenue_growers(
                db, market="TW", limit=10, as_of=as_of,
            )
        )
    except Exception as exc:
        record_error("top_revenue_growers", exc)


async def fetch_active_buybacks(
    ctx: dict[str, Any],
    db: "AsyncSession",
    *,
    as_of: date | None,
    record_error: ErrorRecorder,
) -> None:
    """Active 庫藏股 — companies whose declared buyback execution
    window covers `as_of`. Bullish-signal block ("公司自家正在買回")."""
    try:
        from services.discussion_service import _tag_industry
        from services.ingest.repository import read_active_buybacks
        ctx["active_buybacks"] = _tag_industry(
            await read_active_buybacks(
                db, market="TW", limit=10, as_of=as_of,
            )
        )
    except Exception as exc:
        record_error("active_buybacks", exc)


async def fetch_govt_bank_flow(
    ctx: dict[str, Any],
    db: "AsyncSession",
    *,
    as_of: date | None,
    record_error: ErrorRecorder,
) -> None:
    """Eight-government-bank net buy/sell summed over the last 5
    trading days — quasi-public-sector flow signal ("國家隊昨天進場
    +12 億 / 已連 3 日買超")."""
    try:
        from services.ingest.repository import read_recent_govt_bank_flow
        ctx["govt_bank_flow_5d"] = await read_recent_govt_bank_flow(
            db, market="TW", days=5, as_of=as_of,
        )
    except Exception as exc:
        record_error("govt_bank_flow_5d", exc)


async def fetch_market_institutional(
    ctx: dict[str, Any],
    db: "AsyncSession",
    *,
    as_of: date | None,
    record_error: ErrorRecorder,
) -> None:
    """Full-market 三大法人 (foreign / SITC / dealer) net flow over
    the last 5 trading days. Index-level narrative complement to
    the per-symbol `top_foreign_buyers`."""
    try:
        from services.ingest.repository import read_recent_market_institutional
        ctx["market_institutional_5d"] = await read_recent_market_institutional(
            db, market="TW", days=5, as_of=as_of,
        )
    except Exception as exc:
        record_error("market_institutional_5d", exc)


async def fetch_top_stock_futures_buyers(
    ctx: dict[str, Any],
    db: "AsyncSession",
    *,
    as_of: date | None,
    record_error: ErrorRecorder,
) -> None:
    """Top N 個股期貨 by foreign-investor net-OI growth over the
    last 5 trading days (PR #282). Distinct from `top_foreign_buyers`
    which tracks cash-equity buying — this is the FUTURES side
    where 外資 builds positions overnight before opening cash
    flow. The pattern "外資個股期 2330 連 5 日多單 +1500 口" leads
    the cash-market move by 1-2 days for many TW chip names.

    Read tier auto-falls-back to []  when the cron hasn't run yet
    (fresh deploy / FinMind paywall), in which case the block is
    silently dropped from the prompt rather than rendering as an
    empty array — schema annotation + persona profiles already gate
    the prompt mention behind the block being non-empty.
    """
    try:
        from services.discussion_service import _tag_industry
        from services.ingest.repository import (
            read_top_foreign_stock_futures_buyers,
        )
        ctx["single_stock_futures_oi"] = _tag_industry(
            await read_top_foreign_stock_futures_buyers(
                db, market="TW", days=5, limit=10, as_of=as_of,
            )
        )
    except Exception as exc:
        record_error("single_stock_futures_oi", exc)
