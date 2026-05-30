"""C1-2 from `misty-mixing-harbor.md`: every market-data waterfall tier
failure log site also bumps `waterfall_tier_failed_total{market,
datatype, tier}` so operators can alert per-tier failure rate without
grepping logs.

These tests pin the counter behaviour by triggering the failure
branch in the relevant services and asserting the corresponding
counter delta. Connectors are AsyncMocked at the service module's
import site (mirrors `test_us_market_service_waterfall.py`'s
strategy) so no real upstream is contacted.
"""
from unittest.mock import AsyncMock, patch

import pytest
from prometheus_client import REGISTRY

import services.tw_market_service as tw_svc
import services.us_market_service as us_svc


# ── helpers ────────────────────────────────────────────────────────


def _counter_value(market: str, datatype: str, tier: str) -> float:
    """Read the current `waterfall_tier_failed_total{...}` sample
    from the Prometheus default registry. Returns 0.0 when the
    labelled child has never been touched (Prometheus auto-creates
    the slot only on first `.inc()`)."""
    return (
        REGISTRY.get_sample_value(
            "waterfall_tier_failed_total",
            {"market": market, "datatype": datatype, "tier": tier},
        )
        or 0.0
    )


# ── US quote waterfall ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_us_quote_polygon_primary_failure_bumps_counter():
    before = _counter_value("us", "quote", "polygon")
    with patch.object(us_svc.settings, "POLYGON_API_KEY", "key"), \
         patch.object(us_svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(us_svc, "cache_set_json", AsyncMock()), \
         patch.object(
             us_svc.polygon, "get_quote",
             AsyncMock(side_effect=RuntimeError("rate limit")),
         ), \
         patch.object(
             us_svc.yfinance, "get_quote",
             AsyncMock(return_value={"price": 150.0}),
         ):
        await us_svc.get_quote("AAPL")
    after = _counter_value("us", "quote", "polygon")
    assert after == before + 1


@pytest.mark.asyncio
async def test_us_quote_stooq_fallback_failure_bumps_counter():
    before = _counter_value("us", "quote", "stooq")
    # Polygon disabled so the primary is yfinance; yfinance returns
    # no price → falls through to Stooq; Stooq raises → counter bump.
    with patch.object(us_svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(us_svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(us_svc, "cache_set_json", AsyncMock()), \
         patch.object(
             us_svc.yfinance, "get_quote", AsyncMock(return_value={}),
         ), \
         patch.object(
             us_svc.stooq, "get_quote",
             AsyncMock(side_effect=RuntimeError("blocked")),
         ), \
         patch.object(
             us_svc.finnhub, "get_quote", AsyncMock(return_value={}),
         ):
        await us_svc.get_quote("FAKE")
    after = _counter_value("us", "quote", "stooq")
    assert after == before + 1


@pytest.mark.asyncio
async def test_us_quote_all_sources_failed_bumps_tier_all_counter():
    before = _counter_value("us", "quote", "all")
    with patch.object(us_svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(us_svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(us_svc, "cache_set_json", AsyncMock()), \
         patch.object(
             us_svc.yfinance, "get_quote", AsyncMock(return_value={}),
         ), \
         patch.object(
             us_svc.stooq, "get_quote", AsyncMock(return_value={}),
         ), \
         patch.object(
             us_svc.finnhub, "get_quote", AsyncMock(return_value={}),
         ):
        await us_svc.get_quote("FAKE")
    after = _counter_value("us", "quote", "all")
    assert after == before + 1


# ── TW quote waterfall ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tw_quote_twse_failure_bumps_counter():
    before = _counter_value("tw", "quote", "twse")
    # Force the live path: skip MIS (market closed), TWSE EOD raises.
    # FinMind returns a valid bar so the round still completes — but
    # the TWSE failure must still be counted.
    with patch.object(tw_svc, "_is_tw_market_open", return_value=False), \
         patch.object(
             tw_svc, "_finmind_preferred", AsyncMock(return_value=False),
         ), \
         patch.object(
             tw_svc.twse, "get_realtime_quote",
             AsyncMock(side_effect=RuntimeError("twse 503")),
         ), \
         patch.object(
             tw_svc.finmind, "get_daily_ohlcv",
             AsyncMock(return_value=[
                 {"close": 100, "volume": 1000, "open": 99,
                  "high": 101, "low": 98},
                 {"close": 102, "volume": 1100, "open": 100,
                  "high": 103, "low": 99},
             ]),
         ):
        raw, _src = await tw_svc.fetch_quote_waterfall("2330")
    assert raw is not None  # FinMind recovered
    after = _counter_value("tw", "quote", "twse")
    assert after == before + 1


@pytest.mark.asyncio
async def test_tw_quote_all_sources_failed_bumps_tier_all_counter():
    before = _counter_value("tw", "quote", "all")
    with patch.object(tw_svc, "_is_tw_market_open", return_value=False), \
         patch.object(
             tw_svc, "_finmind_preferred", AsyncMock(return_value=False),
         ), \
         patch.object(
             tw_svc.twse, "get_realtime_quote", AsyncMock(return_value=None),
         ), \
         patch.object(
             tw_svc.finmind, "get_daily_ohlcv", AsyncMock(return_value=[]),
         ):
        raw, _src = await tw_svc.fetch_quote_waterfall("FAKE")
    assert raw is None
    after = _counter_value("tw", "quote", "all")
    assert after == before + 1


# ── US fundamentals + financials ───────────────────────────────────


@pytest.mark.asyncio
async def test_us_fundamentals_polygon_failure_bumps_counter():
    before = _counter_value("us", "fundamentals", "polygon")
    with patch.object(us_svc.settings, "POLYGON_API_KEY", "key"), \
         patch.object(us_svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(us_svc, "cache_set_json", AsyncMock()), \
         patch.object(
             us_svc.polygon, "get_ticker_details",
             AsyncMock(side_effect=RuntimeError("rate limit")),
         ), \
         patch.object(
             us_svc.yfinance, "get_info",
             AsyncMock(return_value={"longName": "Apple Inc."}),
         ):
        await us_svc.get_fundamentals("AAPL")
    after = _counter_value("us", "fundamentals", "polygon")
    assert after == before + 1


@pytest.mark.asyncio
async def test_us_financials_polygon_failure_bumps_counter():
    before = _counter_value("us", "financials", "polygon")
    with patch.object(us_svc.settings, "POLYGON_API_KEY", "key"), \
         patch.object(us_svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(us_svc, "cache_set_json", AsyncMock()), \
         patch.object(
             us_svc.polygon, "get_financials",
             AsyncMock(side_effect=RuntimeError("403")),
         ), \
         patch.object(
             us_svc.yfinance, "get_financials",
             AsyncMock(return_value={
                 "income_statement": [{"revenue": 100}],
                 "balance_sheet": [], "cash_flow": [],
             }),
         ):
        await us_svc.get_financials("AAPL")
    after = _counter_value("us", "financials", "polygon")
    assert after == before + 1


# ── US options ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_us_options_polygon_failure_bumps_counter():
    before = _counter_value("us", "options", "polygon")
    with patch.object(us_svc.settings, "POLYGON_API_KEY", "key"), \
         patch.object(us_svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(us_svc, "cache_set_json", AsyncMock()), \
         patch.object(
             us_svc.polygon, "get_options_chain",
             AsyncMock(side_effect=RuntimeError("403")),
         ), \
         patch.object(
             us_svc.yfinance, "get_options",
             AsyncMock(return_value=[{"contract": "x", "price": 5}]),
         ):
        await us_svc.get_options("AAPL")
    after = _counter_value("us", "options", "polygon")
    assert after == before + 1
