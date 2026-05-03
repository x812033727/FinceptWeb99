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
    """FinMind's `revenue_year` / `revenue_month` are the REVENUE PERIOD
    year and month integers (not YoY / MoM growth rates). We expose
    them as `period_year` / `period_month` so callers don't accidentally
    treat them as percentages — that's what produced the +2026% YoY
    bug fixed in PR #211. Growth rates are computed downstream in the
    ingest task by joining historical rows."""
    rows = [{
        "date":          "2024-04-10",     # report date
        "revenue":       100_000_000,
        "revenue_month": 4,                # April (period month, not %)
        "revenue_year":  2024,             # 2024 (period year, not %)
    }]
    patcher, _ = install_query(rows)
    with patcher:
        out = await finmind.get_monthly_revenue("2330", "2024-04-01")
    assert out[0]["revenue"] == 100_000_000
    assert out[0]["period_month"] == 4
    assert out[0]["period_year"] == 2024
    # Growth rate keys must NOT be present — letting them leak as
    # 4 / 2024 caused the original bug.
    assert "revenue_mom" not in out[0]
    assert "revenue_yoy" not in out[0]
    assert out[0]["symbol"] == "2330"


# ── get_news (TaiwanStockNews) ───────────────────────────────────

@pytest.mark.asyncio
async def test_get_news_loops_day_by_day_when_end_date_set():
    """FinMind's TaiwanStockNews rejects multi-day calls with 400
    ("dataset size is too large, end_date must be None"). `get_news`
    walks the [start, end] window one day at a time with end_date=None,
    accumulates rows. Verifies N _query calls for an N-day range, all
    with end_date=None."""
    rows_by_day = {
        "2024-01-01": [{"title": "d1", "link": "l1",
                        "source": "x", "description": "",
                        "date": "2024-01-01 09:00:00", "stock_id": "2330"}],
        "2024-01-02": [],   # quiet day
        "2024-01-03": [{"title": "d3", "link": "l3",
                        "source": "x", "description": "",
                        "date": "2024-01-03 09:00:00", "stock_id": "2330"}],
    }

    async def _fake_query(dataset, data_id, start_date, end_date=None):
        assert end_date is None, "loop must call _query without end_date"
        return rows_by_day.get(start_date, [])

    with patch.object(finmind, "_query", new=_fake_query):
        out = await finmind.get_news(
            "2024-01-01", symbol="", end_date="2024-01-03",
        )

    titles = [r["title"] for r in out]
    assert titles == ["d1", "d3"]


@pytest.mark.asyncio
async def test_get_news_single_day_passthrough():
    """When `end_date is None` (the live ingest path) OR the range is
    one day (`start == end`) we issue a single `_query` call without
    end_date — no loop overhead."""
    mock = AsyncMock(return_value=[])
    with patch.object(finmind, "_query", new=mock):
        await finmind.get_news("2024-01-01")
    mock.assert_awaited_once()
    # 4th positional arg = end_date, must be None.
    assert mock.call_args.args[3] is None

    mock.reset_mock()
    with patch.object(finmind, "_query", new=mock):
        await finmind.get_news(
            "2024-01-01", symbol="", end_date="2024-01-01",
        )
    mock.assert_awaited_once()
    assert mock.call_args.args[3] is None


@pytest.mark.asyncio
async def test_get_news_per_day_failure_does_not_abort_loop():
    """One bad day shouldn't poison the whole window — log and skip,
    keep going with the rest. Verifies partial results return
    successfully when at least one day worked."""
    err = httpx.HTTPStatusError("HTTP 400", request=None, response=None)

    async def _flaky_query(dataset, data_id, start_date, end_date=None):
        if start_date == "2024-01-02":
            raise err
        return [{"title": f"news {start_date}",
                 "link": f"l-{start_date}",
                 "source": "x", "description": "",
                 "date": f"{start_date} 09:00:00", "stock_id": "2330"}]

    with patch.object(finmind, "_query", new=_flaky_query):
        out = await finmind.get_news(
            "2024-01-01", symbol="", end_date="2024-01-03",
        )

    titles = [r["title"] for r in out]
    assert titles == ["news 2024-01-01", "news 2024-01-03"]


@pytest.mark.asyncio
async def test_get_news_all_days_failing_reraises_last_error():
    """If every day in the range fails, re-raise the last exception
    so the backfill orchestrator surfaces the paywall / rate-limit /
    network error to the user instead of silently returning [] and
    looking like 'no news this window'."""
    err = httpx.HTTPStatusError("HTTP 402", request=None, response=None)

    async def _always_fails(*args, **kwargs):
        raise err

    with patch.object(finmind, "_query", new=_always_fails):
        with pytest.raises(httpx.HTTPStatusError):
            await finmind.get_news(
                "2024-01-01", symbol="", end_date="2024-01-03",
            )


@pytest.mark.asyncio
async def test_get_news_empty_window_when_end_before_start():
    """Caller bug check: end < start returns [] without calling
    _query. Avoids an infinite-style loop on a backwards range."""
    mock = AsyncMock(return_value=[])
    with patch.object(finmind, "_query", new=mock):
        out = await finmind.get_news(
            "2024-01-10", symbol="", end_date="2024-01-01",
        )
    assert out == []
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_news_maps_finmind_fields_to_canonical_shape():
    """FinMind's row uses `source` / `date` / `stock_id`; we expose
    `source_name` / `published_at` / `symbol` so downstream callers
    don't have to know FinMind's column names."""
    rows = [{
        "title":       "台積電 Q2 EPS 創高",
        "link":        "https://example.com/2330",
        "source":      "經濟日報",
        "description": "summary text",
        "date":        "2026-04-15 09:30:00",
        "stock_id":    "2330",
    }]
    patcher, _ = install_query(rows)
    with patcher:
        out = await finmind.get_news("2026-04-01")
    assert out[0]["title"] == "台積電 Q2 EPS 創高"
    assert out[0]["source_name"] == "經濟日報"
    assert out[0]["published_at"] == "2026-04-15 09:30:00"
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
