"""Tests for `event_calendar_service.get_market_upcoming_events`
(PR #284) — the market-wide aggregation layer over the existing
per-symbol yfinance calendar.

Stubs `get_upcoming_event` to return controlled per-symbol
results, then asserts the merge / sort / cap / dedupe behaviour.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from services import event_calendar_service as svc


def _evt(
    earnings_in: int | None = None,
    ex_div_in: int | None = None,
) -> dict:
    """Synthesize the `get_upcoming_event` return shape — values
    that aren't `None` populate the corresponding fields."""
    earnings_visible = earnings_in is not None
    ex_div_visible = ex_div_in is not None
    next_event: str | None
    next_event_in_days: int | None
    if earnings_visible and ex_div_visible:
        if earnings_in <= ex_div_in:
            next_event, next_event_in_days = "earnings", earnings_in
        else:
            next_event, next_event_in_days = "ex_dividend", ex_div_in
    elif earnings_visible:
        next_event, next_event_in_days = "earnings", earnings_in
    elif ex_div_visible:
        next_event, next_event_in_days = "ex_dividend", ex_div_in
    else:
        return None  # type: ignore[return-value]
    return {
        "as_of": "2026-04-15",
        "earnings_date":
            f"2026-04-{15 + earnings_in:02d}" if earnings_visible else None,
        "earnings_in_days":
            earnings_in if earnings_visible else None,
        "ex_dividend_date":
            f"2026-04-{15 + ex_div_in:02d}" if ex_div_visible else None,
        "ex_dividend_in_days":
            ex_div_in if ex_div_visible else None,
        "next_event":         next_event,
        "next_event_in_days": next_event_in_days,
    }


# ── happy path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_market_calendar_aggregates_and_sorts_by_soonest():
    """Three symbols with different next-event distances → result is
    sorted ascending by `next_event_in_days`."""
    fixtures: dict[str, dict | None] = {
        "2330": _evt(earnings_in=5),
        "2454": _evt(earnings_in=2),
        "2317": _evt(ex_div_in=10),
    }

    async def _fake(market, sym, as_of=None, *, lookahead_days=14):
        return fixtures.get(sym)

    with patch.object(svc, "get_upcoming_event", _fake):
        out = await svc.get_market_upcoming_events(
            "TW", ["2330", "2454", "2317"],
        )
    assert [e["symbol"] for e in out] == ["2454", "2330", "2317"]
    assert out[0]["next_event_in_days"] == 2
    # Flattened next_event_date matches the soonest event for each.
    assert out[0]["next_event"] == "earnings"
    assert out[0]["next_event_date"] == "2026-04-17"


@pytest.mark.asyncio
async def test_market_calendar_drops_symbols_without_event():
    """Symbols where the per-symbol lookup returns None (no event in
    window or yfinance offline) are silently excluded — a single
    upstream miss doesn't blank the whole block."""
    fixtures: dict[str, dict | None] = {
        "2330": _evt(earnings_in=5),
        "2454": None,                # no event / lookup failed
        "2317": _evt(ex_div_in=10),
    }

    async def _fake(market, sym, as_of=None, *, lookahead_days=14):
        return fixtures.get(sym)

    with patch.object(svc, "get_upcoming_event", _fake):
        out = await svc.get_market_upcoming_events(
            "TW", ["2330", "2454", "2317"],
        )
    assert [e["symbol"] for e in out] == ["2330", "2317"]


@pytest.mark.asyncio
async def test_market_calendar_dedupes_duplicate_symbols():
    """Caller passes the same symbol twice (e.g. it appears in BOTH
    `top_foreign_buyers` AND `single_stock_futures_oi`). Aggregator
    fans out yfinance ONCE — both for cost reasons and so the
    output doesn't carry duplicate rows."""
    call_count = {"n": 0}

    async def _fake(market, sym, as_of=None, *, lookahead_days=14):
        call_count["n"] += 1
        return _evt(earnings_in=5)

    with patch.object(svc, "get_upcoming_event", _fake):
        out = await svc.get_market_upcoming_events(
            "TW", ["2330", "2330", "2330", "2454"],
        )
    assert [e["symbol"] for e in out] == ["2330", "2454"]
    # Two unique symbols → two yfinance calls, not four.
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_market_calendar_skips_synthetic_symbols():
    """`_TAIEX` / `_TAIEX_TR` carry no calendar — skip without firing
    yfinance to save the round-trip."""
    call_count = {"n": 0}

    async def _fake(market, sym, as_of=None, *, lookahead_days=14):
        call_count["n"] += 1
        return _evt(earnings_in=5)

    with patch.object(svc, "get_upcoming_event", _fake):
        out = await svc.get_market_upcoming_events(
            "TW", ["_TAIEX", "_TAIEX_TR", "2330"],
        )
    assert [e["symbol"] for e in out] == ["2330"]
    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_market_calendar_caps_to_limit():
    """Cap applies AFTER sort, so the result is the N soonest
    events (not just the first N alphabetically)."""
    # 6 symbols; limit=3 → expect the 3 with smallest in_days.
    fixtures: dict[str, dict | None] = {
        f"S{i:02d}": _evt(earnings_in=10 - i)  # decreasing in_days as i grows
        for i in range(6)
    }

    async def _fake(market, sym, as_of=None, *, lookahead_days=14):
        return fixtures.get(sym)

    with patch.object(svc, "get_upcoming_event", _fake):
        out = await svc.get_market_upcoming_events(
            "TW", list(fixtures.keys()), limit=3,
        )
    # Soonest first: i=5 (in_days=5), i=4 (in_days=6), i=3 (in_days=7)
    assert [e["symbol"] for e in out] == ["S05", "S04", "S03"]


@pytest.mark.asyncio
async def test_market_calendar_returns_empty_for_empty_universe():
    """Empty input list → empty output, no calls made."""
    out = await svc.get_market_upcoming_events("TW", [])
    assert out == []


@pytest.mark.asyncio
async def test_market_calendar_stable_sort_ties_by_symbol():
    """Two symbols with the same `next_event_in_days` are
    tie-broken by symbol so audit / signal-trend reads stay
    reproducible across runs."""
    fixtures = {
        "2454": _evt(earnings_in=5),
        "2330": _evt(earnings_in=5),
        "9999": _evt(earnings_in=5),
    }

    async def _fake(market, sym, as_of=None, *, lookahead_days=14):
        return fixtures.get(sym)

    with patch.object(svc, "get_upcoming_event", _fake):
        # Pass in jumbled order to force the tie-break to actually
        # do work (not just preserve insertion order).
        out = await svc.get_market_upcoming_events(
            "TW", ["9999", "2454", "2330"],
        )
    assert [e["symbol"] for e in out] == ["2330", "2454", "9999"]
