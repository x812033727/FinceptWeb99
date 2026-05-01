"""Pure unit tests for the TW market service's don't-cache-empty behavior.

When TWSE + FinMind + MOPS all fail, callers used to lock in a TTL_QUOTE
(60s) of zero-state data. Mirroring `us_market_service.get_quote` we now
skip cache_set when the result is empty so the next request retries.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services import tw_market_service as svc


# ── get_quote ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_quote_zero_price_not_cached():
    """All upstream sources failed ⇒ price=0 ⇒ don't cache."""
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.twse, "get_realtime_quote", AsyncMock(side_effect=RuntimeError)), \
         patch.object(svc.finmind, "get_daily_ohlcv", AsyncMock(return_value=[])):
        result = await svc.get_quote("2330")

    mock_set.assert_not_awaited()
    assert result["price"] == 0


@pytest.mark.asyncio
async def test_get_quote_real_price_is_cached():
    raw = {
        "symbol": "2330", "name_zh": "台積電",
        "close": 820.0, "prev_close": 815.0, "change": 5.0,
        "open": 818.0, "high": 825.0, "low": 815.0, "volume": 25_000_000,
    }
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.twse, "get_realtime_quote", AsyncMock(return_value=raw)):
        result = await svc.get_quote("2330")

    mock_set.assert_awaited_once()
    assert result["price"] == 820.0


# ── get_history ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_history_empty_not_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.twse, "get_daily_ohlcv", AsyncMock(side_effect=RuntimeError)), \
         patch.object(svc.finmind, "get_daily_ohlcv", AsyncMock(return_value=[])):
        result = await svc.get_history("2330", months=1)

    mock_set.assert_not_awaited()
    assert result == []


# ── get_institutional ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_institutional_empty_not_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.finmind, "get_institutional", AsyncMock(return_value=[])), \
         patch.object(svc.twse, "get_institutional", AsyncMock(return_value=[])):
        result = await svc.get_institutional("2330")

    mock_set.assert_not_awaited()
    assert result == []


# ── get_margin ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_margin_empty_not_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.finmind, "get_margin", AsyncMock(return_value=[])), \
         patch.object(svc.twse, "get_margin", AsyncMock(return_value=[])):
        result = await svc.get_margin("2330")

    mock_set.assert_not_awaited()
    assert result == []


# ── get_revenue ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_revenue_empty_not_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.finmind, "get_monthly_revenue", AsyncMock(return_value=[])), \
         patch.object(svc.mops, "get_monthly_revenue_recent", AsyncMock(return_value=[])):
        result = await svc.get_revenue("2330")

    mock_set.assert_not_awaited()
    assert result == []


# ── get_fundamentals ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_fundamentals_no_ratios_not_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.twse, "get_valuation_ratios", AsyncMock(side_effect=RuntimeError)):
        result = await svc.get_fundamentals("2330")

    mock_set.assert_not_awaited()
    assert result["symbol"] == "2330"


@pytest.mark.asyncio
async def test_get_fundamentals_with_ratios_is_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.twse, "get_valuation_ratios", AsyncMock(return_value={"pe": 18.5, "pb": 5.2})):
        result = await svc.get_fundamentals("2330")

    mock_set.assert_awaited_once()
    assert result["pe"] == 18.5
