"""HTTP-bound context blocks.

These four blocks dominate `build_market_context` latency because each
issues outbound HTTP (TWSE / FinMind / FRED / yfinance). They're safe
to fan out via `asyncio.gather` because none of them touch the shared
`db` session — each goes through `*_autosession` service helpers that
open their own DB connection.

Live mode (`as_of=None`): hits live waterfall + Redis cache.
Backtest mode (`as_of=<date>`): each underlying service routes to
`ohlcv_daily` / FRED `observation_end` instead.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any, Callable

log = logging.getLogger(__name__)

ErrorRecorder = Callable[[str, Exception], None]


async def fetch_screener(
    ctx: dict[str, Any],
    *,
    market: str,
    top_n: int,
    as_of: date | None,
    record_error: ErrorRecorder,
) -> None:
    """Top gainers + top losers. Reads from market service screener,
    sorts by `change_pct`, picks the head and tail. Skips speculative
    ETFs (2x leveraged / inverse / futures-tracking) on the TW side
    because they mean-revert the next session."""
    try:
        from services.discussion.screener_utils import (
            _compact_screener_row,
            _compact_us_screener_row,
            _is_speculative_etf,
        )
        if market == "TW":
            from services import tw_market_service
            rows = await tw_market_service.get_screener(
                limit=200, min_volume=1_000_000, as_of=as_of,
            )
            scored = [
                r for r in rows
                if isinstance(r.get("change_pct"), (int, float))
                and not _is_speculative_etf(r.get("symbol"))
            ]
            scored.sort(key=lambda r: r["change_pct"], reverse=True)
            ctx["top_gainers"] = [_compact_screener_row(r) for r in scored[:top_n]]
            ctx["top_losers"] = [_compact_screener_row(r) for r in scored[-top_n:][::-1]]
        elif market == "US":
            from services import us_market_service
            rows = await us_market_service.get_screener(
                limit=200, min_volume=1_000_000, as_of=as_of,
            )
            scored = [
                r for r in rows
                if isinstance(r.get("change_pct"), (int, float))
            ]
            scored.sort(key=lambda r: r["change_pct"], reverse=True)
            ctx["top_gainers"] = [_compact_us_screener_row(r) for r in scored[:top_n]]
            ctx["top_losers"] = [_compact_us_screener_row(r) for r in scored[-top_n:][::-1]]
    except Exception as exc:
        record_error("screener", exc)


async def fetch_index(
    ctx: dict[str, Any],
    *,
    market: str,
    as_of: date | None,
    record_error: ErrorRecorder,
) -> None:
    """Index-level snapshot. TW: TAIEX quote + 30-day history. US:
    SPY/QQQ/DIA quotes (ETF tickers, not `^GSPC` — Polygon free tier
    doesn't serve `^`-prefixed indices). Each US ticker fetched in
    parallel; a single ticker outage still fills the others."""
    try:
        if market == "TW":
            from services import tw_market_service
            ctx["index"] = await tw_market_service.get_index(
                history_days=30, as_of=as_of,
            )
        elif market == "US":
            from services import us_market_service
            tickers = [
                ("SPY", "S&P 500 (SPY)"),
                ("QQQ", "NASDAQ-100 (QQQ)"),
                ("DIA", "Dow Jones (DIA)"),
            ]

            async def _q(t: str):
                try:
                    return await us_market_service.get_quote(t, as_of=as_of)
                except Exception:
                    return None

            results = await asyncio.gather(*[_q(t) for t, _ in tickers])
            index_block: dict[str, Any] = {}
            for (ticker, label), q in zip(tickers, results):
                if q is None or not q:
                    continue
                index_block[ticker] = {
                    "label":      label,
                    "price":      q.get("price"),
                    "change_pct": q.get("change_pct"),
                    "prev_close": q.get("prev_close"),
                }
            ctx["index"] = index_block or None
    except Exception as exc:
        record_error("index", exc)


async def fetch_macro(
    ctx: dict[str, Any],
    *,
    as_of: date | None,
    record_error: ErrorRecorder,
) -> None:
    """FRED macro series — Fed funds / 10Y / yield spread / DXY /
    TWD/USD with 1y + 3m deltas. Always fetched regardless of `market`
    because rates / FX matter everywhere."""
    try:
        from services.discussion_service import _assemble_macro_block
        ctx["macro"] = await _assemble_macro_block(as_of=as_of)
    except Exception as exc:
        record_error("macro", exc)


async def fetch_focus_briefs(
    ctx: dict[str, Any],
    *,
    market: str,
    focus_symbols: list[str] | None,
    as_of: date | None,
    record_error: ErrorRecorder,
) -> None:
    """Per-focus-symbol mini analyst report. Skipped when no focus
    symbols. Each brief includes quote / 52w bands / RSI / moving
    averages so personas can cite real numbers instead of guessing
    from headlines."""
    if not focus_symbols:
        return
    try:
        from services.discussion_service import _assemble_focus_briefs
        ctx["focus_briefs"] = await _assemble_focus_briefs(
            market=market,
            symbols=list(focus_symbols),
            as_of=as_of,
        )
    except Exception as exc:
        record_error("focus_briefs", exc)
