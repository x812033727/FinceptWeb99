from unittest.mock import AsyncMock, patch

import pytest

import services.tw_market_service as tw_service
import services.us_market_service as us_service
from services.quote_consistency import compare_prices


def test_compare_prices_uses_symmetric_spread_and_flags_conflicts():
    verified = compare_prices(
        market="US", primary_source="polygon", primary_price=100,
        secondary_source="yfinance", secondary_price=100.5, max_spread_pct=1,
    )
    assert verified["status"] == "verified"
    assert verified["spread_pct"] == pytest.approx(0.4988)
    assert verified["flags"] == []

    conflict = compare_prices(
        market="US", primary_source="polygon", primary_price=100,
        secondary_source="yfinance", secondary_price=104, max_spread_pct=1,
    )
    assert conflict["status"] == "conflict"
    assert conflict["spread_pct"] == pytest.approx(3.9216)
    assert conflict["flags"] == ["price_source_conflict"]


def test_compare_prices_rejects_non_positive_observations():
    result = compare_prices(
        market="TW", primary_source="twse", primary_price=0,
        secondary_source="finmind", secondary_price=100, max_spread_pct=0.5,
    )
    assert result["status"] == "unverified"
    assert result["flags"] == ["invalid_price_observation"]


@pytest.mark.asyncio
async def test_us_crosscheck_uses_independent_provider_and_detects_conflict():
    quote = {"price": 100, "data_source": "polygon"}
    with patch.object(
        us_service.yfinance, "get_quote", AsyncMock(return_value={"price": 103}),
    ) as secondary:
        result = await us_service.verify_quote_consistency("AAPL", quote)

    secondary.assert_awaited_once_with("AAPL")
    assert result["status"] == "conflict"
    assert result["primary_source"] == "polygon"
    assert result["secondary_source"] == "yfinance"


@pytest.mark.asyncio
async def test_us_crosscheck_does_not_retry_failed_polygon_fallback():
    quote = {"price": 100, "data_source": "yfinance"}
    with patch.object(
        us_service.stooq, "get_quote", AsyncMock(return_value={"price": 100.2}),
    ) as secondary, patch.object(
        us_service.polygon, "get_quote", new_callable=AsyncMock,
    ) as polygon:
        result = await us_service.verify_quote_consistency("AAPL", quote)

    secondary.assert_awaited_once_with("AAPL")
    polygon.assert_not_awaited()
    assert result["status"] == "verified"
    assert result["secondary_source"] == "stooq"


@pytest.mark.asyncio
async def test_tw_crosscheck_defers_during_regular_session():
    quote = {"price": 100, "data_source": "twse_mis"}
    with patch.object(tw_service, "_is_tw_market_open", return_value=True), \
         patch.object(tw_service.finmind, "get_daily_ohlcv", new_callable=AsyncMock) as finmind:
        result = await tw_service.verify_quote_consistency("2330", quote)

    finmind.assert_not_awaited()
    assert result["status"] == "unverified"
    assert result["flags"] == ["crosscheck_deferred_during_regular_session"]


@pytest.mark.asyncio
async def test_tw_crosscheck_compares_settled_quote_with_finmind():
    quote = {"price": 100, "data_source": "twse"}
    bars = [{"close": 100.2}]
    with patch.object(tw_service, "_is_tw_market_open", return_value=False), \
         patch.object(
             tw_service.finmind, "get_daily_ohlcv", AsyncMock(return_value=bars),
         ) as finmind:
        result = await tw_service.verify_quote_consistency("2330", quote)

    finmind.assert_awaited_once()
    assert result["status"] == "verified"
    assert result["secondary_source"] == "finmind"
