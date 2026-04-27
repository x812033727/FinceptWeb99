"""Pure unit tests for the US market service options-chain fallback.

The service used to return [] when POLYGON_API_KEY was unset; now it
falls through to yfinance.get_options() so a free-tier deployment still
serves option chains.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from services import us_market_service as svc


def _yf_chain_row(strike: float = 200.0, ctype: str = "call") -> dict:
    return {
        "ticker": f"AAPL250620C{int(strike * 1000):08d}",
        "underlying_ticker": "AAPL",
        "contract_type": ctype,
        "expiration_date": "2025-06-20",
        "strike_price": strike,
        "last_price": 5.25,
        "bid": 5.20,
        "ask": 5.30,
        "volume": 1000,
        "open_interest": 5000,
        "implied_volatility": 0.28,
        "in_the_money": False,
    }


@pytest.mark.asyncio
async def test_get_options_cache_hit_skips_providers():
    cached = json.dumps([_yf_chain_row()])
    with patch.object(svc, "cache_get", AsyncMock(return_value=cached)), \
         patch.object(svc.polygon, "get_options_chain", AsyncMock()) as mp, \
         patch.object(svc.yfinance, "get_options", AsyncMock()) as my:
        result = await svc.get_options("AAPL")
    mp.assert_not_awaited()
    my.assert_not_awaited()
    assert len(result) == 1


@pytest.mark.asyncio
async def test_get_options_yfinance_fallback_when_no_polygon_key():
    """No Polygon key set ⇒ go straight to yfinance."""
    fallback_rows = [_yf_chain_row(200.0), _yf_chain_row(210.0, "put")]
    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.polygon, "get_options_chain", AsyncMock()) as mp, \
         patch.object(svc.yfinance, "get_options", AsyncMock(return_value=fallback_rows)) as my:
        result = await svc.get_options("AAPL")

    mp.assert_not_awaited()
    my.assert_awaited_once_with("AAPL", None)
    mock_set.assert_awaited_once()
    assert result == fallback_rows


@pytest.mark.asyncio
async def test_get_options_yfinance_fallback_when_polygon_raises():
    """Polygon configured but raised ⇒ fall through to yfinance."""
    fallback_rows = [_yf_chain_row()]
    with patch.object(svc.settings, "POLYGON_API_KEY", "test-key"), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc.polygon, "get_options_chain", AsyncMock(side_effect=RuntimeError("rate limited"))) as mp, \
         patch.object(svc.yfinance, "get_options", AsyncMock(return_value=fallback_rows)) as my:
        result = await svc.get_options("AAPL")

    mp.assert_awaited_once()
    my.assert_awaited_once_with("AAPL", None)
    assert result == fallback_rows


@pytest.mark.asyncio
async def test_get_options_empty_result_not_cached():
    """Both providers returned [] — caching that would lock TTL_OPTIONS of empty."""
    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.yfinance, "get_options", AsyncMock(return_value=[])):
        result = await svc.get_options("FAKE")

    mock_set.assert_not_awaited()
    assert result == []


@pytest.mark.asyncio
async def test_get_options_passes_expiration_date_through():
    fallback_rows = [_yf_chain_row()]
    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc.yfinance, "get_options", AsyncMock(return_value=fallback_rows)) as my:
        await svc.get_options("AAPL", "2025-06-20")
    my.assert_awaited_once_with("AAPL", "2025-06-20")
