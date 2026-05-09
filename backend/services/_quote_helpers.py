"""Shared market-quote helpers used by both TW and US services.

Originally lived in `tw_market_service` but the same protections are
just as relevant for US data: Polygon / yfinance occasionally surface
implausible deltas (split-adjusted vs un-adjusted historicals, ETFs
post-distribution, snapshot-tier stale rows). Keeping the rail and
its log channel in one place means we tune it once.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


# TW stocks have a ±10% daily limit; US has none in theory but
# anything above ±30% on a name-level quote is empirically upstream
# junk (TWSE KY-listed `Change`-mislabeled-as-`prev_close`,
# Polygon split-adjusted historicals divergence, snapshot-tier rows
# written before the rail existed). When tripped we return None so
# callers replace the value with NULL rather than render +992%
# headlines.
DAILY_MOVE_MAX_PCT = 30.0


def sanitize_change_pct(
    symbol: str, market: str, chg_pct: float | None,
) -> float | None:
    """Return ``chg_pct`` if within ``±DAILY_MOVE_MAX_PCT``, else
    ``None``. Logs the rejected value with `symbol` + `market` for
    operator visibility."""
    if chg_pct is None:
        return None
    if abs(chg_pct) > DAILY_MOVE_MAX_PCT:
        log.warning(
            "quote.change_pct_out_of_bounds",
            extra={
                "symbol": symbol, "market": market, "change_pct": chg_pct,
            },
        )
        return None
    return chg_pct


def sanitize_price(
    symbol: str, market: str, price: float | None,
) -> float | None:
    """Return ``price`` if strictly positive, else ``None``.

    Listed equities never trade at 0 or below; a non-positive close
    almost always means an upstream parser hit a placeholder ('—',
    'N/A', etc.) coerced into 0. Letting it through poisons cached
    quotes (chg_pct = -100% headlines) and OHLCV history (zero candles
    that break ATR / volatility math). Caller-side should drop the
    affected row instead of writing it.
    """
    if price is None:
        return None
    if price <= 0:
        log.warning(
            "quote.price_non_positive",
            extra={"symbol": symbol, "market": market, "price": price},
        )
        return None
    return price
