"""Tests for `services.overseas_market_service`.

Unit-level — yfinance is mocked. Covers:
  - Live-mode happy path returns one row per ticker with %change
  - Single ticker connector failure skip-and-continues
  - Backtest mode picks the bar at-or-before `as_of`
  - Backtest with insufficient bars (< 2) skips that ticker
  - Empty result is NOT cached (so transient outages don't lock
    in 5-min zero state)
  - Cache hit short-circuits the connector entirely

Note on mocking: `data.us.yfinance_connector` imports pandas, which
isn't installed in the dev sandbox. We register a stub module in
`sys.modules` BEFORE importing the service so the lazy import
inside the service picks up the stub and the test stays
self-contained. CI has pandas — the stub is just for sandbox
fidelity.
"""
from __future__ import annotations

import sys
import types
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest

# Stub `data.us.yfinance_connector` before the service imports it.
# The real connector pulls in pandas which is absent in the dev
# sandbox; tests mock the connector's surface anyway.
_yfin_stub = types.ModuleType("data.us.yfinance_connector")
_yfin_stub.get_quote = AsyncMock()         # populated per-test
_yfin_stub.get_history = AsyncMock()
sys.modules.setdefault("data.us.yfinance_connector", _yfin_stub)

from services import overseas_market_service as svc   # noqa: E402


def _bar(close: float, when: date) -> dict:
    """yfinance bar shape: ms-since-epoch UTC + OHLCV."""
    ts_ms = int(
        datetime(
            when.year, when.month, when.day, 20, 0, 0, tzinfo=UTC,
        ).timestamp() * 1000
    )
    return {
        "time": ts_ms, "open": close, "high": close,
        "low": close, "close": close, "volume": 0,
    }


# ── live mode ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_overseas_snapshot_live_returns_curated_universe():
    """One row per `_OVERSEAS_INDICES` entry. Each row carries the
    display name, latest close, prev_close, and change_pct."""
    quotes = {
        "^SOX":  {"price": 5400.5, "prev_close": 5500.0, "change_pct": -1.81},
        "^IXIC": {"price": 18000.0, "prev_close": 17900.0, "change_pct": 0.56},
        "^GSPC": {"price": 5800.0, "prev_close": 5750.0, "change_pct": 0.87},
        "^DJI":  {"price": 41500.0, "prev_close": 41400.0, "change_pct": 0.24},
        "^VIX":  {"price": 16.5, "prev_close": 15.0, "change_pct": 10.0},
    }

    async def _fake_quote(ticker: str) -> dict:
        return quotes[ticker]

    with patch("services.overseas_market_service.cache_get",
               AsyncMock(return_value=None)), \
         patch("services.overseas_market_service.cache_set",
               AsyncMock()), \
         patch("data.us.yfinance_connector.get_quote",
               side_effect=_fake_quote):
        out = await svc.get_overseas_snapshot()

    assert out["as_of"] is None
    by_sym = {row["symbol"]: row for row in out["indices"]}
    assert set(by_sym) == set(svc._OVERSEAS_INDICES)
    assert by_sym["^SOX"]["name"] == "PHLX 半導體"
    assert by_sym["^SOX"]["change_pct"] == -1.81
    assert by_sym["^VIX"]["change_pct"] == 10.0


@pytest.mark.asyncio
async def test_get_overseas_snapshot_skips_failing_ticker():
    """Single ticker raising a connector error doesn't blank the
    whole block — we get rows for the others. Mirrors the
    fail-soft pattern used by the chip / news context blocks."""
    async def _fake_quote(ticker: str) -> dict:
        if ticker == "^VIX":
            raise RuntimeError("yfinance IP-blocked for ^VIX")
        return {
            "price": 100.0, "prev_close": 99.0, "change_pct": 1.01,
        }

    with patch("services.overseas_market_service.cache_get",
               AsyncMock(return_value=None)), \
         patch("services.overseas_market_service.cache_set",
               AsyncMock()), \
         patch("data.us.yfinance_connector.get_quote",
               side_effect=_fake_quote):
        out = await svc.get_overseas_snapshot()

    syms = {row["symbol"] for row in out["indices"]}
    assert "^VIX" not in syms
    assert "^SOX" in syms       # other tickers still present
    assert len(out["indices"]) == len(svc._OVERSEAS_INDICES) - 1


@pytest.mark.asyncio
async def test_get_overseas_snapshot_skips_zero_price():
    """yfinance occasionally returns `price=0` on rate-limit or for
    a delisted/unavailable ticker. Skip such rows — they'd render
    as -100% change in the UI."""
    async def _fake_quote(ticker: str) -> dict:
        if ticker == "^DJI":
            return {"price": 0, "prev_close": 0, "change_pct": 0}
        return {
            "price": 100.0, "prev_close": 99.0, "change_pct": 1.01,
        }

    with patch("services.overseas_market_service.cache_get",
               AsyncMock(return_value=None)), \
         patch("services.overseas_market_service.cache_set",
               AsyncMock()), \
         patch("data.us.yfinance_connector.get_quote",
               side_effect=_fake_quote):
        out = await svc.get_overseas_snapshot()

    syms = {row["symbol"] for row in out["indices"]}
    assert "^DJI" not in syms


# ── backtest mode ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_overseas_snapshot_backtest_picks_as_of_bar():
    """Backtest at `as_of` picks the close on/before that date and
    the previous bar's close for the 1-day change. Future bars
    (date > as_of) are excluded — backtest must not see them."""
    as_of = date(2026, 4, 15)
    history_bars = [
        _bar(100.0, date(2026, 4, 13)),
        _bar(102.0, date(2026, 4, 14)),
        _bar(105.0, date(2026, 4, 15)),
        _bar(120.0, date(2026, 4, 16)),   # POST as_of — must NOT leak
        _bar(125.0, date(2026, 4, 17)),
    ]

    async def _fake_history(ticker: str, period: str = "10d",
                            interval: str = "1d") -> list[dict]:
        return history_bars

    with patch("services.overseas_market_service.cache_get",
               AsyncMock(return_value=None)), \
         patch("services.overseas_market_service.cache_set",
               AsyncMock()), \
         patch("data.us.yfinance_connector.get_history",
               side_effect=_fake_history):
        out = await svc.get_overseas_snapshot(as_of=as_of)

    assert out["as_of"] == "2026-04-15"
    sox = next(r for r in out["indices"] if r["symbol"] == "^SOX")
    # Last bar <= 2026-04-15 is the 105 close; previous is 102.
    assert sox["close"] == 105.0
    assert sox["prev_close"] == 102.0
    expected_pct = round((105.0 / 102.0 - 1) * 100, 4)
    assert sox["change_pct"] == expected_pct


@pytest.mark.asyncio
async def test_get_overseas_snapshot_backtest_skips_when_only_one_bar():
    """Less than 2 bars in the as-of window → can't compute change %.
    Skip the row instead of returning an unreliable value."""
    as_of = date(2026, 4, 15)
    history_bars = [_bar(100.0, date(2026, 4, 15))]   # only the as_of bar

    with patch("services.overseas_market_service.cache_get",
               AsyncMock(return_value=None)), \
         patch("services.overseas_market_service.cache_set",
               AsyncMock()), \
         patch("data.us.yfinance_connector.get_history",
               AsyncMock(return_value=history_bars)):
        out = await svc.get_overseas_snapshot(as_of=as_of)

    assert out["indices"] == []


@pytest.mark.asyncio
async def test_get_overseas_snapshot_backtest_skips_zero_prev_close():
    """Defensive: if the prev-day close is 0 (corrupt source row),
    skip the ticker — would otherwise produce a div-by-zero
    masquerading as inf%."""
    as_of = date(2026, 4, 15)
    history_bars = [
        _bar(0.0, date(2026, 4, 14)),
        _bar(100.0, date(2026, 4, 15)),
    ]

    with patch("services.overseas_market_service.cache_get",
               AsyncMock(return_value=None)), \
         patch("services.overseas_market_service.cache_set",
               AsyncMock()), \
         patch("data.us.yfinance_connector.get_history",
               AsyncMock(return_value=history_bars)):
        out = await svc.get_overseas_snapshot(as_of=as_of)

    assert out["indices"] == []


# ── cache behaviour ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_overseas_snapshot_short_circuits_on_cache_hit():
    """A cache hit returns immediately without touching the connector
    — multi-discussion runs in parallel must not stampede yfinance.
    Cache stores JSON-encoded strings; the service decodes on read."""
    import json as _json
    cached_payload = {
        "as_of": None,
        "indices": [{"symbol": "^SOX", "name": "x",
                     "close": 1, "prev_close": 1, "change_pct": 0}],
    }
    quote_mock = AsyncMock()
    with patch("services.overseas_market_service.cache_get",
               AsyncMock(return_value=_json.dumps(cached_payload))), \
         patch("data.us.yfinance_connector.get_quote",
               quote_mock):
        out = await svc.get_overseas_snapshot()

    assert out == cached_payload
    quote_mock.assert_not_called()


@pytest.mark.asyncio
async def test_get_overseas_snapshot_falls_through_on_corrupt_cache():
    """A bad cache row (e.g. left over from a schema change) shouldn't
    poison the response — the service falls through to the live
    fetch, logs, and overwrites the row on success."""
    quote_mock = AsyncMock(return_value={
        "price": 100.0, "prev_close": 99.0, "change_pct": 1.01,
    })
    with patch("services.overseas_market_service.cache_get",
               AsyncMock(return_value="not valid json {")), \
         patch("services.overseas_market_service.cache_set",
               AsyncMock()), \
         patch("data.us.yfinance_connector.get_quote",
               quote_mock):
        out = await svc.get_overseas_snapshot()

    assert len(out["indices"]) > 0
    quote_mock.assert_called()


@pytest.mark.asyncio
async def test_get_overseas_snapshot_does_not_cache_empty_result():
    """When EVERY ticker fails, the empty payload must NOT be
    cached — a transient yfinance outage would otherwise lock 5
    min of zero state. Mirrors the tw_market_service /
    us_market_service "don't cache empty" convention."""
    set_mock = AsyncMock()
    with patch("services.overseas_market_service.cache_get",
               AsyncMock(return_value=None)), \
         patch("services.overseas_market_service.cache_set",
               set_mock), \
         patch("data.us.yfinance_connector.get_quote",
               AsyncMock(side_effect=RuntimeError("upstream down"))):
        out = await svc.get_overseas_snapshot()

    assert out["indices"] == []
    set_mock.assert_not_called()
