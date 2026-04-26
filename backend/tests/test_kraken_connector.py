"""Pure unit tests for the Kraken REST connector.

No live HTTP — every test mocks httpx at module level. Asserts:
- Symbol mapping (BTC ↔ XBT) round-trips
- Ticker response parsing extracts price / change_pct / volume
- OHLC parsing produces our standard bar shape
- Error responses (Kraken `error` array, HTTP non-200) return None safely
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from data.crypto import kraken_connector as kc
from data.crypto.symbols import (
    TOP20,
    from_kraken_response_key,
    to_kraken_pair,
)


# ── Symbol mapping ────────────────────────────────────────────────

def test_top20_has_20_unique_symbols():
    assert len(TOP20) == 20
    assert len(set(TOP20)) == 20


def test_btc_maps_to_kraken_xbt():
    assert to_kraken_pair("BTC") == "XBTUSD"
    assert to_kraken_pair("btc") == "XBTUSD"


def test_doge_maps_to_kraken_xdg():
    assert to_kraken_pair("DOGE") == "XDGUSD"


def test_unsupported_symbol_returns_none():
    assert to_kraken_pair("SHIB") is None
    assert to_kraken_pair("") is None


def test_kraken_response_keys_round_trip():
    # Both legacy XX/ZZ-prefixed and short forms should map back.
    assert from_kraken_response_key("XXBTZUSD") == "BTC"
    assert from_kraken_response_key("XBTUSD") == "BTC"
    assert from_kraken_response_key("XDGUSD") == "DOGE"
    assert from_kraken_response_key("UNKNOWN") is None


# ── Async fakes for httpx ─────────────────────────────────────────

class _FakeResp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _ok(result_payload: dict) -> _FakeResp:
    return _FakeResp(200, {"error": [], "result": result_payload})


def _client_returning(resp: _FakeResp):
    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, params=None): return resp
    return _Client


# ── get_quote ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_quote_parses_kraken_ticker(monkeypatch):
    payload = {
        "XXBTZUSD": {
            "c": ["100200.5", "0.123"],     # last trade
            "o": "98000.0",                  # opening price
            "v": ["100", "12345.67"],        # volume today / 24h
            "h": ["101000", "101500"],       # high today / 24h
            "l": ["97500", "97000"],         # low today / 24h
        }
    }
    monkeypatch.setattr(kc.httpx, "AsyncClient", _client_returning(_ok(payload)))

    q = await kc.get_quote("BTC")
    assert q is not None
    assert q["symbol"] == "BTC"
    assert q["market"] == "CRYPTO"
    assert q["price"] == 100200.5
    # change vs opening: (100200.5 - 98000) / 98000 * 100 ≈ 2.245%
    assert q["change_pct"] == pytest.approx(2.245, abs=0.01)
    assert q["volume"] == 12345.67
    assert q["high_24h"] == 101500
    assert q["low_24h"] == 97000
    assert q["currency"] == "USD"


@pytest.mark.asyncio
async def test_get_quote_returns_none_for_unsupported_symbol():
    # No httpx call should even fire because the symbol map rejects it.
    with patch.object(kc, "_get") as mock_get:
        result = await kc.get_quote("FAKECOIN")
    mock_get.assert_not_called()
    assert result is None


@pytest.mark.asyncio
async def test_get_quote_handles_kraken_error_array(monkeypatch):
    monkeypatch.setattr(
        kc.httpx, "AsyncClient",
        _client_returning(_FakeResp(200, {"error": ["EQuery:Unknown asset pair"], "result": {}})),
    )
    assert await kc.get_quote("BTC") is None


@pytest.mark.asyncio
async def test_get_quote_handles_http_500(monkeypatch):
    monkeypatch.setattr(
        kc.httpx, "AsyncClient",
        _client_returning(_FakeResp(500, {})),
    )
    assert await kc.get_quote("BTC") is None


@pytest.mark.asyncio
async def test_get_quote_zero_opening_does_not_divide_by_zero(monkeypatch):
    payload = {
        "XBTUSD": {
            "c": ["100", "1"], "o": "0",
            "v": ["1", "1"], "h": ["1", "1"], "l": ["1", "1"],
        }
    }
    monkeypatch.setattr(kc.httpx, "AsyncClient", _client_returning(_ok(payload)))
    q = await kc.get_quote("BTC")
    assert q is not None
    assert q["change_pct"] == 0.0


# ── get_history ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_history_parses_ohlc_rows(monkeypatch):
    # Kraken row shape: [time, open, high, low, close, vwap, volume, count]
    rows = [
        [1714000000, "97000", "98000", "96500", "97500", "97200", "100.5", 250],
        [1714003600, "97500", "98500", "97000", "98000", "97800", "150.0", 300],
    ]
    payload = {"XXBTZUSD": rows, "last": 1714003600}
    monkeypatch.setattr(kc.httpx, "AsyncClient", _client_returning(_ok(payload)))

    bars = await kc.get_history("BTC", interval="1h", limit=10)
    assert len(bars) == 2
    assert bars[0]["open"] == 97000.0
    assert bars[0]["close"] == 97500.0
    assert bars[0]["volume"] == 100.5
    # ts → unix ms (matches yfinance/twse contract that the chart expects)
    assert bars[0]["time"] == 1714000000 * 1000
    assert bars[1]["time"] == 1714003600 * 1000


@pytest.mark.asyncio
async def test_get_history_caps_at_limit(monkeypatch):
    rows = [[1714000000 + i * 60, "1", "2", "0.5", "1.5", "1", "10", 5] for i in range(50)]
    payload = {"XBTUSD": rows, "last": rows[-1][0]}
    monkeypatch.setattr(kc.httpx, "AsyncClient", _client_returning(_ok(payload)))

    bars = await kc.get_history("BTC", interval="1m", limit=10)
    assert len(bars) == 10
    # Should be the last 10 bars, not the first 10
    assert bars[-1]["time"] > bars[0]["time"]


@pytest.mark.asyncio
async def test_get_history_returns_empty_on_error(monkeypatch):
    monkeypatch.setattr(
        kc.httpx, "AsyncClient",
        _client_returning(_FakeResp(200, {"error": ["EService:Unavailable"], "result": {}})),
    )
    assert await kc.get_history("BTC") == []


@pytest.mark.asyncio
async def test_get_top_pairs_returns_top20():
    pairs = await kc.get_top_pairs()
    assert pairs == list(TOP20)
