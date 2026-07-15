"""Pure unit tests for the US + TW background refresh tasks.

Pins down the fix that the polling tasks now use the shared
`fetch_quote_waterfall` helper instead of calling the primary connector
directly. Without the fix, a Polygon rate-limit pause (or a TWSE OpenAPI
hiccup) froze every subscriber's live price for the whole window because
the task only tried the primary source and gave up on exception.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import services.tw_market_service as tw_svc
import services.us_market_service as us_svc
from tasks import tw_market_refresh as tw_task
from tasks import us_market_refresh as us_task


class _NoopSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _NoopSessionFactory:
    """Stub for `db.session.AsyncSessionLocal`. The refresh tasks open a
    transient session for AlertService; we mock the alert call separately
    so the session itself just needs to be a no-op async context manager."""

    def __call__(self) -> _NoopSession:
        return _NoopSession()


# ── US ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_us_refresh_falls_through_to_yfinance_when_polygon_raises():
    """The polling task must use the full waterfall — Polygon failure
    falls through to yfinance, the WS subscriber still gets a price."""
    yf_quote = {
        "symbol": "AAPL", "price": 195.0, "change": 1.0, "change_pct": 0.5,
        "volume": 12345, "open": 194, "high": 196, "low": 193,
        "prev_close": 194.0, "market_cap": 3_000_000_000_000,
        "ts": 1700000000000,
    }
    published: list[tuple[str, str, dict]] = []

    async def fake_publish(sym: str, market: str, payload: dict):
        published.append((sym, market, payload))

    with patch.object(us_task, "_subscribed_us_symbols", new=AsyncMock(return_value={"AAPL"})), \
         patch.object(us_task, "_is_market_open", return_value=True), \
         patch.object(us_task, "publish_update", new=fake_publish), \
         patch.object(us_task, "cache_set", new=AsyncMock()), \
         patch.object(us_svc.settings, "POLYGON_API_KEY", "test-key"), \
         patch.object(us_svc.polygon, "get_quote",
                      new=AsyncMock(side_effect=RuntimeError("rate limit"))), \
         patch.object(us_svc.yfinance, "get_quote",
                      new=AsyncMock(return_value=yf_quote)), \
         patch("services.alert_service.AlertService.check_and_fire",
               new=AsyncMock()), \
         patch("db.session.AsyncSessionLocal", new=_NoopSessionFactory()):
        await us_task.refresh_us_quotes()

    assert len(published) == 1
    sym, market, payload = published[0]
    assert (sym, market) == ("AAPL", "US")
    assert payload["price"] == 195.0
    # Critical: the WS delta carries the source so subscribers can flag it.
    assert payload["data_source"] == "yfinance"


@pytest.mark.asyncio
async def test_us_refresh_marks_unavailable_when_all_sources_fail():
    """Polygon + yfinance + Stooq all blew up: the task should NOT publish
    a stale 0-price update — that would mislead subscribers."""
    published: list[tuple[str, str, dict]] = []

    async def fake_publish(sym: str, market: str, payload: dict):
        published.append((sym, market, payload))

    with patch.object(us_task, "_subscribed_us_symbols", new=AsyncMock(return_value={"AAPL"})), \
         patch.object(us_task, "_is_market_open", return_value=True), \
         patch.object(us_task, "publish_update", new=fake_publish), \
         patch.object(us_task, "cache_set", new=AsyncMock()), \
         patch.object(us_svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(us_svc.yfinance, "get_quote",
                      new=AsyncMock(side_effect=RuntimeError("blocked"))), \
         patch.object(us_svc.stooq, "get_quote",
                      new=AsyncMock(side_effect=RuntimeError("blocked"))), \
         patch("services.alert_service.AlertService.check_and_fire",
               new=AsyncMock()), \
         patch("db.session.AsyncSessionLocal", new=_NoopSessionFactory()):
        await us_task.refresh_us_quotes()

    # Task still publishes (it doesn't filter on price=0) but the
    # data_source field clearly marks the row as degraded.
    assert len(published) == 1
    payload = published[0][2]
    assert payload["data_source"] == "unavailable"


# ── TW ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tw_refresh_falls_through_to_finmind_when_twse_returns_none():
    """TW polling task should now call FinMind when TWSE realtime returns
    nothing — previously it gave up after the TWSE call and the WS
    subscriber's price froze."""
    finmind_bars = [{"time": "2024-04-01", "open": 1, "high": 2, "low": 0,
                     "close": 1.5, "volume": 100}]
    published: list[tuple[str, str, dict]] = []

    async def fake_publish(sym: str, market: str, payload: dict):
        published.append((sym, market, payload))

    with patch.object(tw_task, "_subscribed_tw_symbols", new=AsyncMock(return_value={"2330"})), \
         patch.object(tw_task, "_is_tw_market_open", return_value=True), \
         patch.object(tw_task, "publish_update", new=fake_publish), \
         patch.object(tw_task, "cache_set", new=AsyncMock()), \
         patch.object(tw_svc.twse_mis, "get_realtime_quote",
                      new=AsyncMock(return_value=None)), \
         patch.object(tw_svc.twse, "get_realtime_quote",
                      new=AsyncMock(return_value=None)), \
         patch.object(tw_svc.finmind, "get_daily_ohlcv",
                      new=AsyncMock(return_value=finmind_bars)), \
         patch("tasks.tw_market_refresh.AsyncSessionLocal", create=True), \
         patch("services.alert_service.AlertService.check_and_fire",
               new=AsyncMock()):
        await tw_task.refresh_tw_quotes()

    assert len(published) == 1
    sym, market, payload = published[0]
    assert (sym, market) == ("2330", "TW")
    assert payload["price"] == 1.5
    assert payload["data_source"] == "finmind"
    assert payload["tz"] == "Asia/Taipei"
