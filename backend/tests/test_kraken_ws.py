"""Unit tests for the Kraken WS pump — focuses on pure helpers + the
message handler. We don't actually open a WebSocket; the connect /
reconnect loop is exercised by integration testing."""
from __future__ import annotations

import json

import pytest

from data.crypto.kraken_ws import (
    KrakenTickerPump,
    _from_v2_pair,
    _to_v2_pair,
)


# ── Pair translation ─────────────────────────────────────────────

def test_to_v2_pair_inserts_slash():
    assert _to_v2_pair("XBTUSD") == "XBT/USD"
    assert _to_v2_pair("ETHUSD") == "ETH/USD"
    assert _to_v2_pair("XDGUSD") == "XDG/USD"


def test_to_v2_pair_passthrough_non_usd():
    # We only quote USD pairs; non-USD inputs return as-is (defensive).
    assert _to_v2_pair("BTCBTC") == "BTCBTC"


def test_from_v2_pair_handles_legacy_xbt():
    assert _from_v2_pair("XBT/USD") == "BTC"
    assert _from_v2_pair("XDG/USD") == "DOGE"


def test_from_v2_pair_handles_modern_naming():
    assert _from_v2_pair("ETH/USD") == "ETH"
    assert _from_v2_pair("SOL/USD") == "SOL"


def test_from_v2_pair_unsupported_returns_none():
    assert _from_v2_pair("SHIB/USD") is None
    assert _from_v2_pair("nopair") is None


# ── Message handler ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_message_dispatches_ticker_to_callback():
    received = []

    async def fake_callback(symbol: str, market: str, payload: dict):
        received.append((symbol, market, payload))

    pump = KrakenTickerPump(fake_callback)
    msg = json.dumps({
        "channel": "ticker",
        "type": "update",
        "data": [
            {"symbol": "XBT/USD", "last": 100200.5, "change_pct": 2.34, "volume": 12345.67},
            {"symbol": "ETH/USD", "last": 3500.0, "change_pct": -0.5, "volume": 9876.5},
        ],
    })

    await pump._handle_message(msg)

    assert len(received) == 2
    assert received[0] == ("BTC", "CRYPTO", {"price": 100200.5, "change_pct": 2.34, "volume": 12345.67})
    assert received[1] == ("ETH", "CRYPTO", {"price": 3500.0, "change_pct": -0.5, "volume": 9876.5})


@pytest.mark.asyncio
async def test_handle_message_ignores_non_ticker_channels():
    received = []

    async def fake_callback(symbol, market, payload):
        received.append((symbol, market, payload))

    pump = KrakenTickerPump(fake_callback)
    await pump._handle_message(json.dumps({"channel": "trade", "data": []}))
    await pump._handle_message(json.dumps({"channel": "heartbeat"}))
    assert received == []


@pytest.mark.asyncio
async def test_handle_message_swallows_malformed_json():
    pump = KrakenTickerPump(lambda *a, **k: None)  # type: ignore[arg-type]
    # Should not raise.
    await pump._handle_message("not-json")
    await pump._handle_message("")


@pytest.mark.asyncio
async def test_handle_message_skips_unknown_pair():
    received = []

    async def fake_callback(symbol, market, payload):
        received.append((symbol, market, payload))

    pump = KrakenTickerPump(fake_callback)
    msg = json.dumps({
        "channel": "ticker",
        "data": [{"symbol": "FAKECOIN/USD", "last": 1, "change_pct": 0, "volume": 0}],
    })
    await pump._handle_message(msg)
    assert received == []


@pytest.mark.asyncio
async def test_handle_message_callback_failure_does_not_propagate():
    """One bad tick must not kill the pump. The handler should swallow
    callback exceptions and continue."""

    async def angry_callback(*a, **k):
        raise RuntimeError("downstream broken")

    pump = KrakenTickerPump(angry_callback)
    msg = json.dumps({
        "channel": "ticker",
        "data": [{"symbol": "XBT/USD", "last": 1, "change_pct": 0, "volume": 0}],
    })
    # Should NOT raise.
    await pump._handle_message(msg)
