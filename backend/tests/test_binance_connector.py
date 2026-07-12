"""Unit tests for data.crypto.binance_connector — row shaping over a
mocked HTTP layer (no live Binance). Live connectivity is exercised
separately by the ingest smoke, not in CI."""
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

import data.crypto.binance_connector as binance


@pytest.mark.asyncio
async def test_fetch_ohlcv_shapes_kline_arrays():
    # Two Binance 12-field kline arrays (openTime ms, o,h,l,c, vol,
    # closeTime, quoteVol, trades, ...).
    klines = [
        [1767225600000, "87648.21", "88919.45", "87550.43", "88839.04",
         "6279.57", 1767311999999, "552991635.29", 1449281, "0", "0", "0"],
        [1767312000000, "88839.04", "90000.00", "88000.00", "89500.00",
         "5000.00", 1767398399999, "440000000.00", 1200000, "0", "0", "0"],
    ]
    # One page then empty (short page ends pagination).
    with patch.object(binance, "_get", new=AsyncMock(side_effect=[klines])):
        rows = await binance.fetch_ohlcv("BTCUSDT", "1d", date(2026, 1, 1), date(2026, 1, 2))

    assert len(rows) == 2
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["interval"] == "1d"
    assert rows[0]["ts"] == "2026-01-01T00:00:00+00:00"
    assert rows[0]["close"] == "88839.04"
    assert rows[0]["quote_volume"] == "552991635.29"
    assert rows[0]["trades"] == 1449281


@pytest.mark.asyncio
async def test_fetch_ohlcv_bad_interval_returns_empty():
    with patch.object(binance, "_get", new=AsyncMock()) as m:
        rows = await binance.fetch_ohlcv("BTCUSDT", "5m", date(2026, 1, 1), date(2026, 1, 2))
    assert rows == []
    m.assert_not_awaited()  # rejected before any HTTP call


@pytest.mark.asyncio
async def test_fetch_funding_rate_shapes_rows():
    page = [
        {"symbol": "BTCUSDT", "fundingTime": 1767225600008,
         "fundingRate": "0.00010000", "markPrice": "87608.30"},
    ]
    with patch.object(binance, "_get", new=AsyncMock(side_effect=[page])):
        rows = await binance.fetch_funding_rate("BTCUSDT", date(2026, 1, 1), date(2026, 1, 1))
    assert len(rows) == 1
    assert rows[0]["funding_rate"] == "0.00010000"
    assert rows[0]["funding_time"].startswith("2026-01-01T00:00:00")


@pytest.mark.asyncio
async def test_get_spot_usdt_symbols_filters_trading_usdt():
    body = {"symbols": [
        {"symbol": "BTCUSDT", "quoteAsset": "USDT", "status": "TRADING"},
        {"symbol": "ETHBTC", "quoteAsset": "BTC", "status": "TRADING"},
        {"symbol": "OLDUSDT", "quoteAsset": "USDT", "status": "BREAK"},
    ]}
    with patch.object(binance, "_get", new=AsyncMock(return_value=body)):
        out = await binance.get_spot_usdt_symbols()
    assert out == {"BTCUSDT"}


@pytest.mark.asyncio
async def test_get_klines_stops_on_short_page():
    """A page shorter than the 1000 limit ends pagination — no infinite
    loop, no second call."""
    short = [[1767225600000, "1", "2", "0.5", "1.5", "10", 1767311999999,
              "15", 5, "0", "0", "0"]]
    m = AsyncMock(side_effect=[short])
    with patch.object(binance, "_get", new=m):
        rows = await binance.get_klines("BTCUSDT", "1d", 0, 10**13)
    assert len(rows) == 1
    assert m.await_count == 1
