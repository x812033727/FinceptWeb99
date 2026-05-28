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
    because they mean-revert the next session.

    Each row stamped with `as_of_session` so personas can see the
    trading session the price belongs to. TW live screener pulls
    `STOCK_DAY_ALL` which only refreshes post-14:30 Taipei — during
    intraday the rows are yesterday's close, and the per-row stamp
    makes that explicit instead of letting the persona re-anchor to
    wall-clock today.
    """
    try:
        from services.discussion.freshness import (
            tw_quote_session,
            us_quote_is_intraday,
            us_quote_session,
        )
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
            # TW screener uses STOCK_DAY_ALL in live mode (post-14:30
            # Taipei refresh) or `ohlcv_daily` clamped to `<= as_of`
            # in backtest. Either way the row is a SESSION CLOSE, not
            # intraday — `is_intraday=False` always for this block.
            sess = (
                as_of.isoformat() if as_of is not None
                else tw_quote_session()
            )
            scored = [
                r for r in rows
                if isinstance(r.get("change_pct"), (int, float))
                and not _is_speculative_etf(r.get("symbol"))
            ]
            scored.sort(key=lambda r: r["change_pct"], reverse=True)
            ctx["top_gainers"] = [
                _compact_screener_row(r, as_of_session=sess, is_intraday=False)
                for r in scored[:top_n]
            ]
            ctx["top_losers"] = [
                _compact_screener_row(r, as_of_session=sess, is_intraday=False)
                for r in scored[-top_n:][::-1]
            ]
        elif market == "US":
            from services import us_market_service
            rows = await us_market_service.get_screener(
                limit=200, min_volume=1_000_000, as_of=as_of,
            )
            # US screener: Polygon snapshot is intraday during NYSE
            # session; yfinance / Stooq fallbacks serve last close.
            # Use `data_source` to pick the correct freshness label
            # per row when present, fall back to the market-wide
            # phase otherwise.
            if as_of is not None:
                sess_default = as_of.isoformat()
                intraday_default = False
            else:
                sess_default = us_quote_session()
                intraday_default = us_quote_is_intraday()
            scored = [
                r for r in rows
                if isinstance(r.get("change_pct"), (int, float))
            ]
            scored.sort(key=lambda r: r["change_pct"], reverse=True)
            ctx["top_gainers"] = [
                _compact_us_screener_row(
                    r,
                    as_of_session=sess_default,
                    is_intraday=intraday_default,
                )
                for r in scored[:top_n]
            ]
            ctx["top_losers"] = [
                _compact_us_screener_row(
                    r,
                    as_of_session=sess_default,
                    is_intraday=intraday_default,
                )
                for r in scored[-top_n:][::-1]
            ]
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
    parallel; a single ticker outage still fills the others.

    Stamps `as_of_session` + `is_intraday` so personas know whether
    `index.value` is truly real-time (TW `MI_5MINS` intraday) or a
    cached close (post-13:30 Taipei). TAIEX history is bar-stamped
    (each bar has its own date), so we additionally surface
    `history_last_session` for quick reading.
    """
    try:
        from services.discussion.freshness import (
            tw_index_is_intraday,
            tw_index_session,
            us_quote_is_intraday,
            us_quote_session,
        )
        if market == "TW":
            from services import tw_market_service
            index = await tw_market_service.get_index(
                history_days=30, as_of=as_of,
            )
            if index:
                history = index.get("history") or []
                history_last = (
                    history[-1].get("time") if history else None
                )
                if as_of is not None:
                    index["as_of_session"] = as_of.isoformat()
                    index["is_intraday"] = False
                else:
                    index["is_intraday"] = tw_index_is_intraday()
                    # `MI_5MINS` is intraday during TWSE regular
                    # session, so the value reflects today; after
                    # the 13:30 close it still carries today's final
                    # value WITHOUT waiting for STOCK_DAY_ALL's
                    # 14:30 publish. So index's session is today as
                    # soon as the market opens — distinct from
                    # `tw_quote_session()` (used by screener /
                    # focus_briefs.quote) which can't anchor on
                    # today until 14:30.
                    index["as_of_session"] = tw_index_session()
                index["history_last_session"] = history_last
            ctx["index"] = index
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
            if as_of is not None:
                sess_default = as_of.isoformat()
                intraday_default = False
            else:
                sess_default = us_quote_session()
                intraday_default = us_quote_is_intraday()
            index_block: dict[str, Any] = {}
            for (ticker, label), q in zip(tickers, results):
                if q is None or not q:
                    continue
                index_block[ticker] = {
                    "label":      label,
                    "price":      q.get("price"),
                    "change_pct": q.get("change_pct"),
                    "prev_close": q.get("prev_close"),
                    "as_of_session": (
                        q.get("as_of") if as_of is not None
                        else sess_default
                    ),
                    "is_intraday": (
                        False if as_of is not None else intraday_default
                    ),
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
