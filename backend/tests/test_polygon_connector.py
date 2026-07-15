"""
Unit tests for data.us.polygon_connector.

Polygon's snapshot endpoint exposes a price-priority chain
(lastTrade → day → prevDay → 0) that the connector flattens into a
single quote dict. The aggregates endpoint returns OHLCV bars under
`results`. Reference / financials / options / snapshot-all all unwrap
their respective payload fields.

httpx.AsyncClient is mocked at the connector's import-site so no
network is hit. The connector reads settings.POLYGON_API_KEY when
constructing the client; we don't assert on the API key being passed
here because it's a configuration detail, not a behaviour.
"""
from unittest.mock import patch

import httpx
import pytest

import data.us.polygon_connector as polygon


# ── Test doubles ──────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, payload: dict | list, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None
            )


class FakeClient:
    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, path: str, params: dict | None = None):
        self.calls.append((path, params or {}))
        return self.responder(path, params or {})


def install_client(responder):
    fake = FakeClient(responder)
    return patch.object(polygon.httpx, "AsyncClient", lambda **_: fake), fake


# ── get_quote ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_quote_uses_last_trade_price_when_available():
    """Price priority: lastTrade.p > day.c > prevDay.c > 0."""
    payload = {
        "ticker": {
            "lastTrade": {"p": 204.5},
            "day": {"c": 203, "o": 200, "h": 205, "l": 199, "v": 1000},
            "prevDay": {"c": 200},
            "details": {"market_cap": 3.2e12},
            "updated": 1700000000000,
        }
    }

    patcher, fake = install_client(lambda *_a, **_k: FakeResponse(payload))
    with patcher:
        q = await polygon.get_quote("aapl")

    assert q["price"] == 204.5
    assert q["prev_close"] == 200
    assert q["change"] == 4.5
    assert q["change_pct"] == 2.25
    assert q["volume"] == 1000
    assert q["open"] == 200
    assert q["market_cap"] == 3.2e12
    assert q["ts"] == 1700000000000
    # Ticker is upper-cased on the way out and on the URL.
    assert q["symbol"] == "AAPL"
    assert "AAPL" in fake.calls[0][0]


@pytest.mark.asyncio
async def test_get_quote_falls_back_to_day_close_then_prev():
    payload = {
        "ticker": {
            "lastTrade": {},  # no trade price
            "day": {"c": 0},  # no day close either
            "prevDay": {"c": 198},
        }
    }
    patcher, _ = install_client(lambda *_a, **_k: FakeResponse(payload))
    with patcher:
        q = await polygon.get_quote("AAPL")
    # Price falls all the way through to prev_close
    assert q["price"] == 198
    # change / change_pct relative to prev_close are 0 because price == prev
    assert q["change"] == 0
    assert q["change_pct"] == 0


@pytest.mark.asyncio
async def test_get_quote_change_is_zero_when_prev_close_is_missing():
    """No prev_close → division would be undefined; connector returns 0."""
    payload = {"ticker": {"lastTrade": {"p": 100}, "day": {}, "prevDay": {}}}
    patcher, _ = install_client(lambda *_a, **_k: FakeResponse(payload))
    with patcher:
        q = await polygon.get_quote("X")
    assert q["price"] == 100
    assert q["prev_close"] == 0
    assert q["change_pct"] == 0


@pytest.mark.asyncio
async def test_get_quote_propagates_http_errors():
    patcher, _ = install_client(lambda *_a, **_k: FakeResponse({}, status_code=429))
    with patcher, pytest.raises(httpx.HTTPStatusError):
        await polygon.get_quote("AAPL")


# ── get_aggs ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_aggs_normalises_bars_and_passes_url_segments():
    payload = {
        "results": [
            {"t": 1700000000000, "o": 100, "h": 102, "l": 99, "c": 101, "v": 5000},
            {"t": 1700086400000, "o": 101, "h": 103, "l": 100, "c": 102, "v": 6000},
        ]
    }
    patcher, fake = install_client(lambda *_a, **_k: FakeResponse(payload))
    with patcher:
        bars = await polygon.get_aggs("aapl", "2024-01-01", "2024-01-02", timespan="day")

    assert len(bars) == 2
    assert bars[0] == {"time": 1700000000000, "open": 100, "high": 102, "low": 99, "close": 101, "volume": 5000}
    # Path includes upper-case ticker, multiplier=1, timespan=day, dates.
    path = fake.calls[0][0]
    assert "/v2/aggs/ticker/AAPL/range/1/day/2024-01-01/2024-01-02" in path
    # Default params: adjusted=true, sort=asc, limit=5000.
    params = fake.calls[0][1]
    assert params["adjusted"] == "true"
    assert params["sort"] == "asc"
    assert params["limit"] == 5000


@pytest.mark.asyncio
async def test_get_aggs_returns_empty_list_when_results_missing():
    patcher, _ = install_client(lambda *_a, **_k: FakeResponse({"status": "OK"}))
    with patcher:
        bars = await polygon.get_aggs("X", "2024-01-01", "2024-01-02")
    assert bars == []


@pytest.mark.asyncio
async def test_get_aggs_passes_adjusted_false_through():
    patcher, fake = install_client(lambda *_a, **_k: FakeResponse({"results": []}))
    with patcher:
        await polygon.get_aggs("X", "2024-01-01", "2024-01-02", adjusted=False)
    assert fake.calls[0][1]["adjusted"] == "false"


# ── reference / financials / options ──────────────────────────────

@pytest.mark.asyncio
async def test_get_ticker_details_unwraps_results_object():
    patcher, _ = install_client(lambda *_a, **_k: FakeResponse(
        {"results": {"name": "Apple Inc.", "market_cap": 3.2e12}}
    ))
    with patcher:
        d = await polygon.get_ticker_details("AAPL")
    assert d["name"] == "Apple Inc."


@pytest.mark.asyncio
async def test_get_ticker_details_returns_empty_dict_when_results_absent():
    patcher, _ = install_client(lambda *_a, **_k: FakeResponse({}))
    with patcher:
        d = await polygon.get_ticker_details("X")
    assert d == {}


@pytest.mark.asyncio
async def test_get_financials_returns_results_list():
    patcher, _ = install_client(lambda *_a, **_k: FakeResponse(
        {"results": [{"fiscal_year": "2024"}, {"fiscal_year": "2023"}]}
    ))
    with patcher:
        f = await polygon.get_financials("AAPL")
    assert len(f) == 2
    assert f[0]["fiscal_year"] == "2024"


@pytest.mark.asyncio
async def test_get_options_chain_filters_by_expiration_when_given():
    payload = {"results": [{
        "ticker": "O:AAPL241220C00200000",
        "details": {"contract_type": "call", "expiration_date": "2024-12-20", "strike_price": 200},
        "last_quote": {"bid": 4.9, "ask": 5.1},
        "last_trade": {"price": 5.0},
        "session": {"volume": 123},
        "open_interest": 456,
        "implied_volatility": 0.25,
        "greeks": {"delta": 0.51, "gamma": 0.02},
        "underlying_asset": {"ticker": "AAPL", "price": 201},
    }]}
    patcher, fake = install_client(lambda *_a, **_k: FakeResponse(payload))
    with patcher:
        opts = await polygon.get_options_chain("AAPL", "2024-12-20")

    assert len(opts) == 1
    assert opts[0]["strike_price"] == 200
    assert opts[0]["bid"] == 4.9
    assert opts[0]["implied_volatility"] == 0.25
    assert opts[0]["open_interest"] == 456
    assert opts[0]["underlying_price"] == 201
    assert opts[0]["delta"] == 0.51
    assert fake.calls[0][0] == "/v3/snapshot/options/AAPL"
    params = fake.calls[0][1]
    assert params["expiration_date"] == "2024-12-20"


@pytest.mark.asyncio
async def test_get_options_chain_omits_expiration_when_none():
    patcher, fake = install_client(lambda *_a, **_k: FakeResponse({"results": []}))
    with patcher:
        await polygon.get_options_chain("AAPL")
    assert "expiration_date" not in fake.calls[0][1]


@pytest.mark.asyncio
async def test_get_options_chain_follows_pagination_with_hard_page_cap():
    calls = 0

    def responder(_path, _params):
        nonlocal calls
        calls += 1
        return FakeResponse({
            "results": [{
                "ticker": f"O:AAPL{calls}",
                "details": {"contract_type": "put", "expiration_date": "2026-08-21", "strike_price": calls},
            }],
            "next_url": f"https://api.polygon.io/next/{calls}",
        })

    patcher, fake = install_client(responder)
    with patcher:
        rows = await polygon.get_options_chain("AAPL")

    assert len(rows) == 4
    assert all(row["chain_truncated"] is True for row in rows)
    assert len(fake.calls) == 4
    assert fake.calls[1][0] == "https://api.polygon.io/next/1"
    assert fake.calls[1][1] == {}


# ── get_snapshot_all (screener) ───────────────────────────────────

@pytest.mark.asyncio
async def test_get_snapshot_all_returns_tickers_array():
    patcher, _ = install_client(lambda *_a, **_k: FakeResponse(
        {"tickers": [{"ticker": "AAPL"}, {"ticker": "MSFT"}]}
    ))
    with patcher:
        snaps = await polygon.get_snapshot_all()
    assert len(snaps) == 2
    assert snaps[0]["ticker"] == "AAPL"


@pytest.mark.asyncio
async def test_get_snapshot_all_filters_by_tickers_when_given():
    patcher, fake = install_client(lambda *_a, **_k: FakeResponse({"tickers": []}))
    with patcher:
        await polygon.get_snapshot_all(["aapl", "msft", "nvda"])

    params = fake.calls[0][1]
    assert params["tickers"] == "AAPL,MSFT,NVDA"  # upper-cased + comma-joined
    assert params["include_otc"] == "false"


@pytest.mark.asyncio
async def test_get_snapshot_all_returns_empty_list_when_field_absent():
    patcher, _ = install_client(lambda *_a, **_k: FakeResponse({"status": "OK"}))
    with patcher:
        snaps = await polygon.get_snapshot_all()
    assert snaps == []
