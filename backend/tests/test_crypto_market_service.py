"""Pure unit tests for the crypto market service.

Service is a thin cache + fan-out layer over data.crypto.kraken_connector.
We mock the connector and the redis cache helpers to verify:
  - cache hits short-circuit Kraken calls
  - cache misses populate the cache
  - get_screener fans out to all Top 20 and sorts by volume desc
  - get_quote returns a stub (not exception) for unsupported symbol
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services import crypto_market_service as svc


# ── get_quote ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_quote_cache_hit_skips_kraken():
    cached_payload = {"symbol": "BTC", "price": 100.0, "currency": "USD"}
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=cached_payload)), \
         patch.object(svc, "_k_quote", AsyncMock()) as mock_quote:
        result = await svc.get_quote("BTC")

    mock_quote.assert_not_awaited()
    assert result["price"] == 100.0


@pytest.mark.asyncio
async def test_get_quote_cache_miss_calls_kraken_and_caches():
    fresh = {"symbol": "BTC", "market": "CRYPTO", "price": 99000.0,
             "change_pct": 1.5, "volume": 100, "currency": "USD"}
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", AsyncMock()) as mock_set, \
         patch.object(svc, "_k_quote", AsyncMock(return_value=fresh)):
        result = await svc.get_quote("BTC")

    mock_set.assert_awaited_once()
    assert result["price"] == 99000.0


@pytest.mark.asyncio
async def test_get_quote_unsupported_symbol_returns_stub_not_exception():
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "_k_quote", AsyncMock(return_value=None)):
        result = await svc.get_quote("FAKECOIN")
    assert result["price"] is None
    assert "error" in result
    assert result["market"] == "CRYPTO"
    assert result["data_source"] == "unavailable"


@pytest.mark.asyncio
async def test_get_quote_cache_miss_tags_data_source_kraken():
    fresh = {"symbol": "BTC", "market": "CRYPTO", "price": 99000.0,
             "change_pct": 1.5, "volume": 100, "currency": "USD"}
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", AsyncMock()), \
         patch.object(svc, "_k_quote", AsyncMock(return_value=fresh)):
        result = await svc.get_quote("BTC")
    assert result["data_source"] == "kraken"
    assert isinstance(result["ts"], int)


# ── get_history ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_history_cache_hit_skips_kraken():
    # Bar shape mirrors yfinance/twse: {time: <unix_ms>, open, high, low, close, volume}
    cached_bars = [{"time": 1714003600000, "close": 90000}]
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=cached_bars)), \
         patch.object(svc, "_k_history", AsyncMock()) as mock_hist:
        result = await svc.get_history("BTC")

    mock_hist.assert_not_awaited()
    assert len(result) == 1
    assert result[0]["close"] == 90000


@pytest.mark.asyncio
async def test_get_history_cache_miss_calls_kraken_and_caches():
    bars = [{"time": 1714003600000, "close": 95000}]
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json_unless_empty", AsyncMock()) as mock_set, \
         patch.object(svc, "_k_history", AsyncMock(return_value=bars)):
        result = await svc.get_history("BTC", interval="1h", limit=100)

    mock_set.assert_awaited_once()
    assert result == bars


@pytest.mark.asyncio
async def test_get_history_empty_result_not_cached():
    """A Kraken outage shouldn't lock the next 5 min into an empty chart."""
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json_unless_empty", AsyncMock()) as mock_set, \
         patch.object(svc, "_k_history", AsyncMock(return_value=[])):
        result = await svc.get_history("BTC")
    # Helper is called but skips the write when value is falsy. Verifying
    # the call shape catches a future refactor that swaps in cache_set_json.
    mock_set.assert_awaited_once()
    args = mock_set.call_args.args
    assert args[1] == []  # the empty list reaches the unless_empty guard
    assert result == []


# ── get_screener ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_screener_sorts_by_volume_desc():
    """Mock get_quote so each symbol returns a different volume; verify
    the result is sorted volume-descending."""
    quotes_by_symbol = {
        "BTC":  {"symbol": "BTC", "price": 100, "change_pct": 1, "volume": 9000},
        "ETH":  {"symbol": "ETH", "price": 50,  "change_pct": 2, "volume": 5000},
        "SOL":  {"symbol": "SOL", "price": 30,  "change_pct": -1, "volume": 7000},
    }

    async def fake_get_quote(sym: str):
        return quotes_by_symbol.get(sym, {"symbol": sym, "price": 1,
                                          "change_pct": 0, "volume": 1})

    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json_unless_empty", AsyncMock()), \
         patch.object(svc, "get_quote", new=fake_get_quote):
        rows = await svc.get_screener(limit=20)

    # All 20 fanned out, sorted desc — BTC (9000) > SOL (7000) > ETH (5000) > 17 stubs (1)
    assert rows[0]["symbol"] == "BTC"
    assert rows[1]["symbol"] == "SOL"
    assert rows[2]["symbol"] == "ETH"
    assert len(rows) == 20


@pytest.mark.asyncio
async def test_get_screener_skips_quotes_with_no_price():
    async def fake_get_quote(sym: str):
        if sym in {"BTC", "ETH"}:
            return {"symbol": sym, "price": 100, "change_pct": 0, "volume": 1}
        return {"symbol": sym, "price": None, "change_pct": None, "volume": None}

    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json_unless_empty", AsyncMock()), \
         patch.object(svc, "get_quote", new=fake_get_quote):
        rows = await svc.get_screener(limit=20)

    assert {r["symbol"] for r in rows} == {"BTC", "ETH"}


@pytest.mark.asyncio
async def test_get_screener_cache_hit_returns_cached():
    cached = [{"symbol": "BTC", "price": 100, "volume": 1000}]
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=cached)), \
         patch.object(svc, "get_quote", AsyncMock()) as mock_quote:
        rows = await svc.get_screener()
    mock_quote.assert_not_awaited()
    assert rows[0]["symbol"] == "BTC"


# ── search ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_substring_match():
    results = await svc.search("BT")
    syms = [r["symbol"] for r in results]
    assert "BTC" in syms


@pytest.mark.asyncio
async def test_search_case_insensitive():
    results = await svc.search("eth")
    assert any(r["symbol"] == "ETH" for r in results)


@pytest.mark.asyncio
async def test_search_empty_query_returns_empty():
    assert await svc.search("") == []
    assert await svc.search("   ") == []
