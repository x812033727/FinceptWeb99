"""
Unit tests for data.us.finnhub_connector.

Covers the connector contract used by the service waterfall:
  - empty FINNHUB_API_KEY → returns {} without hitting the network
  - happy-path JSON parse maps c/d/dp/h/l/o/pc to our shared shape
  - Finnhub's all-zero "unknown symbol" response → return {}
  - rate-limit (HTTP 429) and arbitrary HTTP failures degrade silently
  - get_batch_quotes fans out concurrently and merges results

httpx.AsyncClient is patched so no network is touched. Settings.FINNHUB_API_KEY
is overridden per-test via patch.object on the connector's `settings` ref.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

import data.us.finnhub_connector as finnhub


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400 and self.status_code != 429:
            raise Exception(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class FakeClient:
    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, url: str, params: dict | None = None):
        params = params or {}
        self.calls.append((url, params))
        return self.responder(params.get("symbol", ""))


def _install(responder):
    fake = FakeClient(responder)
    return patch.object(finnhub.httpx, "AsyncClient", lambda **_: fake), fake


# ── disabled gate ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_quote_returns_empty_when_disabled():
    """Empty resolved key must short-circuit before any HTTP call."""
    with patch.object(finnhub, "_active_key", AsyncMock(return_value="")):
        # If the connector tried to hit httpx, this would raise (no patch).
        result = await finnhub.get_quote("AAPL")
    assert result == {}


@pytest.mark.asyncio
async def test_get_batch_quotes_returns_empty_when_disabled():
    with patch.object(finnhub, "_active_key", AsyncMock(return_value="")):
        result = await finnhub.get_batch_quotes(["AAPL", "MSFT"])
    assert result == {}


@pytest.mark.asyncio
async def test_get_batch_quotes_returns_empty_when_no_tickers():
    # No tickers ⇒ short-circuit even before resolving the key (avoid the
    # DB / cache hit when the caller passes an empty list).
    with patch.object(finnhub, "_active_key", AsyncMock(return_value="k")):
        result = await finnhub.get_batch_quotes([])
    assert result == {}


# ── _row_to_quote: parsing edge cases ─────────────────────────────

def test_row_to_quote_maps_full_payload():
    row = {"c": 182.5, "d": 1.2, "dp": 0.66, "h": 183, "l": 181, "o": 182, "pc": 181.3, "t": 1719939600}
    q = finnhub._row_to_quote(row, "aapl")
    assert q is not None
    assert q["symbol"] == "AAPL"
    assert q["price"] == 182.5
    assert q["change"] == 1.2
    assert q["change_pct"] == 0.66
    assert q["prev_close"] == 181.3
    assert q["high"] == 183
    assert q["low"] == 181
    assert q["open"] == 182


def test_row_to_quote_returns_none_when_current_zero():
    """Finnhub returns 0 for every field on unknown symbols / unsupported
    indices on the free plan. That must NOT be surfaced as a real quote."""
    row = {"c": 0, "d": 0, "dp": 0, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0}
    assert finnhub._row_to_quote(row, "FOO") is None


def test_row_to_quote_returns_none_when_current_missing():
    assert finnhub._row_to_quote({}, "FOO") is None


def test_row_to_quote_derives_change_when_only_pc_present():
    """Some Finnhub responses omit `d` / `dp` — derive from `c` and `pc`."""
    row = {"c": 200.0, "pc": 100.0, "h": 0, "l": 0, "o": 0}
    q = finnhub._row_to_quote(row, "FOO")
    assert q is not None
    assert q["change"] == 100.0
    assert q["change_pct"] == 100.0


# ── get_quote: HTTP behaviour ─────────────────────────────────────

@pytest.mark.asyncio
async def test_get_quote_returns_parsed_payload_on_success():
    payload = {"c": 182.5, "d": 1.2, "dp": 0.66, "h": 183, "l": 181, "o": 182, "pc": 181.3}
    patcher, _ = _install(lambda _sym: FakeResponse(payload))
    with patch.object(finnhub, "_active_key", AsyncMock(return_value="key")), patcher:
        result = await finnhub.get_quote("AAPL")
    assert result["symbol"] == "AAPL"
    assert result["price"] == 182.5


@pytest.mark.asyncio
async def test_get_quote_drops_unknown_symbol_zero_response():
    payload = {"c": 0, "d": 0, "dp": 0, "h": 0, "l": 0, "o": 0, "pc": 0}
    patcher, _ = _install(lambda _sym: FakeResponse(payload))
    with patch.object(finnhub, "_active_key", AsyncMock(return_value="key")), patcher:
        result = await finnhub.get_quote("FOOBAR")
    assert result == {}


@pytest.mark.asyncio
async def test_get_quote_returns_empty_on_rate_limit():
    """HTTP 429 → empty dict so the service waterfall continues without
    holding the request open. Must not raise."""
    patcher, _ = _install(lambda _sym: FakeResponse({}, status_code=429))
    with patch.object(finnhub, "_active_key", AsyncMock(return_value="key")), patcher:
        result = await finnhub.get_quote("AAPL")
    assert result == {}


@pytest.mark.asyncio
async def test_get_quote_returns_empty_on_http_error():
    patcher, _ = _install(lambda _sym: FakeResponse({}, status_code=500))
    with patch.object(finnhub, "_active_key", AsyncMock(return_value="key")), patcher:
        result = await finnhub.get_quote("AAPL")
    assert result == {}


# ── get_batch_quotes: concurrency + missing rows ──────────────────

@pytest.mark.asyncio
async def test_get_batch_quotes_merges_responses():
    payloads = {
        "AAPL": {"c": 180, "pc": 178, "d": 2, "dp": 1.12, "h": 0, "l": 0, "o": 0},
        "MSFT": {"c": 410, "pc": 405, "d": 5, "dp": 1.23, "h": 0, "l": 0, "o": 0},
    }
    patcher, _ = _install(lambda sym: FakeResponse(payloads[sym]))
    with patch.object(finnhub, "_active_key", AsyncMock(return_value="key")), patcher:
        out = await finnhub.get_batch_quotes(["AAPL", "MSFT"])
    assert set(out.keys()) == {"AAPL", "MSFT"}
    assert out["AAPL"]["price"] == 180
    assert out["MSFT"]["price"] == 410


@pytest.mark.asyncio
async def test_get_batch_quotes_skips_unknown_symbols():
    """Unknown symbols return {c:0,...} and must be filtered out — the
    caller treats absence as 'no upstream data'."""
    def responder(sym: str) -> FakeResponse:
        if sym == "AAPL":
            return FakeResponse({"c": 180, "pc": 178, "d": 2, "dp": 1.12, "h": 0, "l": 0, "o": 0})
        return FakeResponse({"c": 0, "pc": 0, "d": 0, "dp": 0, "h": 0, "l": 0, "o": 0})

    patcher, _ = _install(responder)
    with patch.object(finnhub, "_active_key", AsyncMock(return_value="key")), patcher:
        out = await finnhub.get_batch_quotes(["AAPL", "FAKE"])
    assert list(out.keys()) == ["AAPL"]
