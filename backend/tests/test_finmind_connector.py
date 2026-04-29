"""
Unit tests for data.tw.finmind_connector.

Two-tier strategy mirroring the TWSE tests:

1. `_query` is the only function that talks to httpx + Redis. We test
   it directly with both layers mocked so the quota gate (a Redis
   counter) and the API's `status != 200` envelope behaviour are
   pinned down.
2. Every higher-level method (`get_daily_ohlcv`, `get_institutional`,
   etc.) only shapes the rows returned by `_query`. We mock `_query`
   itself and assert on the field-mapping / pivot logic.
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import data.tw.finmind_connector as finmind


# Patch `_get_token` for every test in this module so the connector
# doesn't transitively import `services.market_key_service` (which pulls
# in `cryptography` and panics on this dev env's missing `_cffi_backend`).
# Individual tests that care about token routing can override.
@pytest.fixture(autouse=True)
def _patch_get_token():
    with patch.object(finmind, "_get_token", AsyncMock(return_value="test-token")):
        yield


# ── Test doubles for _query's HTTP layer ─────────────────────────

class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
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
    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, url, params=None):
        self.calls.append((url, params or {}))
        return self.response


def install_http(response):
    fake = FakeClient(response)
    return patch.object(finmind.httpx, "AsyncClient", lambda **_: fake), fake


# ── _query: quota gate ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_query_returns_empty_when_hourly_quota_exhausted():
    """Quota counter > limit → no HTTP request fires, [] returned."""
    limit = finmind.settings.FINMIND_HOURLY_REQUEST_LIMIT

    patcher_http, fake = install_http(FakeResponse({"status": 200, "data": [{"x": 1}]}))
    with patcher_http, patch.object(
        finmind, "cache_incr", new=AsyncMock(return_value=limit + 1)
    ):
        out = await finmind._query("TaiwanStockPrice", "2330", "2024-01-01")

    assert out == []
    # Critically: the HTTP request must NOT have been made.
    assert fake.calls == []


@pytest.mark.asyncio
async def test_query_proceeds_when_quota_count_at_or_below_limit():
    limit = finmind.settings.FINMIND_HOURLY_REQUEST_LIMIT
    payload = {"status": 200, "data": [{"date": "2024-01-02", "close": 785}]}

    patcher_http, fake = install_http(FakeResponse(payload))
    with patcher_http, patch.object(
        finmind, "cache_incr", new=AsyncMock(return_value=limit)
    ):
        out = await finmind._query("TaiwanStockPrice", "2330", "2024-01-01")

    assert out == [{"date": "2024-01-02", "close": 785}]
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_query_uses_one_hour_ttl_on_counter():
    """Counter TTL must match FinMind's per-hour rate limit window
    (3600s) — a 24h TTL would waste 24× the available budget."""
    payload = {"status": 200, "data": []}
    patcher_http, _ = install_http(FakeResponse(payload))
    cache_mock = AsyncMock(return_value=1)
    with patcher_http, patch.object(finmind, "cache_incr", cache_mock):
        await finmind._query("TaiwanStockPrice", "2330", "2024-01-01")

    _, kwargs = cache_mock.call_args
    assert kwargs.get("ttl_seconds") == 3600


# ── _query: API envelope behaviour ───────────────────────────────

@pytest.mark.asyncio
async def test_query_returns_empty_when_envelope_status_is_not_200():
    """FinMind wraps errors as `{"status": 402, "msg": "..."}` with HTTP
    200 — connector must not surface those rows."""
    patcher_http, _ = install_http(FakeResponse({"status": 402, "msg": "quota"}))
    with patcher_http, patch.object(finmind, "cache_incr", new=AsyncMock(return_value=1)):
        out = await finmind._query("TaiwanStockPrice", "2330", "2024-01-01")
    assert out == []


@pytest.mark.asyncio
async def test_query_returns_empty_when_data_field_is_missing():
    """`{"status": 200}` with no `data` field → []."""
    patcher_http, _ = install_http(FakeResponse({"status": 200}))
    with patcher_http, patch.object(finmind, "cache_incr", new=AsyncMock(return_value=1)):
        out = await finmind._query("TaiwanStockPrice", "2330", "2024-01-01")
    assert out == []


# ── _query: parameter wiring ─────────────────────────────────────

@pytest.mark.asyncio
async def test_query_passes_dataset_data_id_dates_and_token():
    payload = {"status": 200, "data": []}
    patcher_http, fake = install_http(FakeResponse(payload))
    with patcher_http, patch.object(finmind, "cache_incr", new=AsyncMock(return_value=1)):
        await finmind._query("TaiwanStockPrice", "2330", "2024-01-01", "2024-04-01")

    _, params = fake.calls[0]
    assert params["dataset"] == "TaiwanStockPrice"
    assert params["data_id"] == "2330"
    assert params["start_date"] == "2024-01-01"
    assert params["end_date"] == "2024-04-01"
    assert "token" in params


@pytest.mark.asyncio
async def test_query_omits_end_date_when_not_provided():
    payload = {"status": 200, "data": []}
    patcher_http, fake = install_http(FakeResponse(payload))
    with patcher_http, patch.object(finmind, "cache_incr", new=AsyncMock(return_value=1)):
        await finmind._query("TaiwanStockPrice", "2330", "2024-01-01")
    _, params = fake.calls[0]
    assert "end_date" not in params


# ── Helper: install _query mock for high-level tests ─────────────

def install_query(rows):
    mock = AsyncMock(return_value=rows)
    return patch.object(finmind, "_query", new=mock), mock


# ── get_daily_ohlcv ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_daily_ohlcv_maps_finmind_field_names_to_canonical():
    """FinMind's price endpoint uses `max` / `min` / `Trading_Volume`
    instead of `high` / `low` / `volume`."""
    rows = [
        {"date": "2024-04-01", "open": 780, "max": 790, "min": 775,
         "close": 785, "Trading_Volume": 1234567},
    ]
    patcher, _ = install_query(rows)
    with patcher:
        out = await finmind.get_daily_ohlcv("2330", "2024-04-01")

    assert out == [
        {"time": "2024-04-01", "open": 780, "high": 790, "low": 775,
         "close": 785, "volume": 1234567},
    ]


@pytest.mark.asyncio
async def test_get_daily_ohlcv_defaults_volume_to_zero_when_missing():
    rows = [{"date": "2024-04-01", "open": 1, "max": 2, "min": 0, "close": 1}]
    patcher, _ = install_query(rows)
    with patcher:
        out = await finmind.get_daily_ohlcv("2330", "2024-04-01")
    assert out[0]["volume"] == 0


# ── get_institutional ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_institutional_pivots_per_investor_rows_into_one_per_date():
    """FinMind returns ONE row per investor type per date (long format).
    Connector pivots to ONE row per date with all six totals."""
    rows = [
        {"date": "2024-04-01", "name": "外資自營商", "buy": 1000, "sell": 500},
        {"date": "2024-04-01", "name": "投信",       "buy": 200,  "sell": 100},
        {"date": "2024-04-01", "name": "自營商",     "buy": 50,   "sell": 20},
        {"date": "2024-04-02", "name": "外資",       "buy": 2000, "sell": 800},
    ]
    patcher, _ = install_query(rows)
    with patcher:
        out = await finmind.get_institutional("2330", "2024-04-01")

    # Sorted by date, one row per date.
    assert len(out) == 2
    assert out[0]["date"] == "2024-04-01"
    assert out[0]["fini_buy"] == 1000
    assert out[0]["sitc_buy"] == 200
    assert out[0]["dealer_buy"] == 50
    assert out[1]["date"] == "2024-04-02"
    assert out[1]["fini_buy"] == 2000


@pytest.mark.asyncio
async def test_get_institutional_drops_unknown_investor_names_silently():
    """An investor name FinMind starts emitting that the connector
    doesn't recognise (e.g. a 4th category) shouldn't break the pivot —
    it just doesn't get mapped to a column. Same date still flows
    through with whatever WAS recognised."""
    rows = [
        {"date": "2024-04-01", "name": "外資", "buy": 100, "sell": 50},
        {"date": "2024-04-01", "name": "私募基金",  "buy": 999, "sell": 999},
    ]
    patcher, _ = install_query(rows)
    with patcher:
        out = await finmind.get_institutional("2330", "2024-04-01")
    assert out[0]["fini_buy"] == 100
    assert "私募基金" not in str(out[0])


# ── get_margin ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_margin_maps_finmind_field_names():
    rows = [{
        "date": "2024-04-01",
        "MarginPurchaseBuy": 1000,
        "MarginPurchaseBalance": 5000,
        "ShortSaleSell": 200,
        "ShortSaleBalance": 1000,
    }]
    patcher, _ = install_query(rows)
    with patcher:
        out = await finmind.get_margin("2330", "2024-04-01")
    assert out[0]["margin_purchase"] == 1000
    assert out[0]["margin_balance"] == 5000
    assert out[0]["short_sale"] == 200
    assert out[0]["short_balance"] == 1000
    assert out[0]["symbol"] == "2330"


@pytest.mark.asyncio
async def test_get_margin_defaults_missing_fields_to_zero():
    rows = [{"date": "2024-04-01"}]  # all margin fields missing
    patcher, _ = install_query(rows)
    with patcher:
        out = await finmind.get_margin("2330", "2024-04-01")
    assert out[0]["margin_purchase"] == 0
    assert out[0]["short_balance"] == 0


# ── get_monthly_revenue ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_monthly_revenue_maps_to_canonical_field_names():
    rows = [{
        "date": "2024-04-01",
        "revenue": 100_000_000,
        "revenue_month": 5.5,
        "revenue_year": 12.3,
    }]
    patcher, _ = install_query(rows)
    with patcher:
        out = await finmind.get_monthly_revenue("2330", "2024-04-01")
    assert out[0]["revenue"] == 100_000_000
    assert out[0]["revenue_mom"] == 5.5
    assert out[0]["revenue_yoy"] == 12.3
    assert out[0]["symbol"] == "2330"


# ── Pass-through methods ─────────────────────────────────────────

@pytest.mark.parametrize("method,dataset", [
    ("get_financials", "TaiwanStockFinancialStatements"),
    ("get_balance_sheet", "TaiwanStockBalanceSheet"),
    ("get_cash_flow", "TaiwanStockCashFlowsStatement"),
    ("get_dividends", "TaiwanStockDividend"),
    ("get_etf_holdings", "TaiwanStockHoldingSharesPer"),
])
@pytest.mark.asyncio
async def test_pass_through_methods_call_query_with_correct_dataset(method, dataset):
    """Five pass-through methods just forward to _query with their
    dataset name + a fixed default start_date. Verify the dataset
    string doesn't drift."""
    patcher, mock = install_query([{"date": "2024-01-01"}])
    with patcher:
        await getattr(finmind, method)("2330")
    args, _ = mock.call_args
    assert args[0] == dataset
    assert args[1] == "2330"
