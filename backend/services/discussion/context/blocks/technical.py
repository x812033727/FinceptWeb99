"""Per-symbol technical signals context block (DB-bound).

Reads `ohlcv_daily` and computes Tier-1 short-term indicators
(volume_ratio, return_5d, return_20d, rsi_14, gap_pct) for each
focus symbol. Markets with an OHLCV archive (TW today; US, crypto
in future) all use the same compute path — no market-specific code
in this block.

Symbols whose archive doesn't yet have >= 21 trading days of data
(fresh listing, gap in ingest) are silently skipped rather than
emitting partial signals — `compute_short_term_signals` returns
None and we just don't add the symbol's entry. Personas read an
empty `short_term_signals` block as "no technical context for the
focus symbols", same as if the block weren't enabled at all.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

ErrorRecorder = Callable[[str, Exception], None]


async def fetch_short_term_signals(
    ctx: dict[str, Any],
    db: "AsyncSession",
    *,
    market: str,
    focus_symbols: list[str] | None,
    as_of: date | None,
    record_error: ErrorRecorder,
    max_focus_symbols: int = 5,
) -> None:
    """Populate `ctx["short_term_signals"]` with per-symbol Tier-1
    metrics. Bounded by `max_focus_symbols` to keep the prompt
    budget linear in the number of mentioned tickers.

    Empty `focus_symbols` → no-op (no per-symbol context to compute).
    Per-symbol failure logs into `ctx["errors"]` but never raises so
    one symbol's missing data can't blank the others.
    """
    if not focus_symbols:
        return
    try:
        from services.ingest.repository import read_symbol_day_trading_trend
        from services.short_term_signals import (
            compute_industry_rs, compute_short_term_signals,
        )
        for sym in focus_symbols[:max_focus_symbols]:
            try:
                signals = await compute_short_term_signals(
                    db, market=market, symbol=sym, as_of=as_of,
                )
            except Exception as exc:
                record_error(f"short_term_signals:{sym}", exc)
                continue
            if signals is None:
                continue
            # Industry RS lives under the same per-symbol key so the
            # persona reads one cohesive technical block per ticker.
            # Failure (no industry classified, too few peers) folds
            # the field into None — partial signals beat no signals.
            try:
                rs = await compute_industry_rs(
                    db, market=market, symbol=sym, as_of=as_of,
                )
            except Exception as exc:
                record_error(f"industry_rs:{sym}", exc)
                rs = None
            signals["industry_rs"] = rs
            # Per-symbol day-trading trend (TW only — table is
            # `TwStockDayTradingDaily`). Other markets fold to None.
            day_trading: dict | None = None
            if market == "TW":
                try:
                    day_trading = await read_symbol_day_trading_trend(
                        db, market=market, symbol=sym, as_of=as_of,
                    )
                except Exception as exc:
                    record_error(f"day_trading_trend:{sym}", exc)
            signals["day_trading_trend"] = day_trading
            ctx["short_term_signals"][sym] = signals
    except Exception as exc:
        record_error("short_term_signals", exc)
