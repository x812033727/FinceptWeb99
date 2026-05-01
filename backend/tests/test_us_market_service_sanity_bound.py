"""Sanity-bound tests for `us_market_service._normalize_quote`.

The TW service has had a ±30% rail since PR #142 to defend against
TWSE's KY-listed quirk that puts the prior-day close in the `Change`
field (yielding +992% deltas). Polygon / yfinance / Stooq / Finnhub
have hit the same class of glitch in production (split-adjusted vs
un-adjusted historicals divergence, post-distribution ETFs, snapshot
rows written before backfill). Tier-2 cleanup PR ⑧ shares the bound
with US.
"""
from __future__ import annotations

from services import us_market_service as svc


def test_normalize_quote_drops_implausibly_large_change_pct():
    """A `change_pct` beyond ±30% is treated as upstream junk and
    dropped — UI shows 0 in the field instead of a misleading +992%
    headline."""
    out = svc._normalize_quote("AAPL", {
        "price": 195.0, "change": 175.0, "change_pct": 992.5,
        "volume": 1_000_000,
    })
    assert out["change_pct"] == 0
    assert out["change"] == 0


def test_normalize_quote_keeps_legal_limit_move():
    """A real ±10–25% move (rare but legitimate, e.g. earnings gap)
    stays within the rail and is preserved."""
    out = svc._normalize_quote("AAPL", {
        "price": 220.0, "change": 22.0, "change_pct": 11.0,
        "volume": 5_000_000,
    })
    assert out["change_pct"] == 11.0
    assert out["change"] == 22.0


def test_normalize_quote_drops_negative_implausible_change():
    """Same rail on the downside — a -50% computed pct is upstream
    junk (no clean US single-name moves that big in a single day
    without a corporate event we'd want to flag explicitly)."""
    out = svc._normalize_quote("XYZ", {
        "price": 5.0, "change": -10.0, "change_pct": -66.67,
    })
    assert out["change_pct"] == 0
    assert out["change"] == 0


def test_normalize_quote_passes_through_zero_change():
    """A flat day: change_pct=0 must pass through (the rail check
    is `abs(x) > 30`, not `>= 0`)."""
    out = svc._normalize_quote("AAPL", {
        "price": 195.0, "change": 0.0, "change_pct": 0.0,
    })
    assert out["change_pct"] == 0
    assert out["change"] == 0
