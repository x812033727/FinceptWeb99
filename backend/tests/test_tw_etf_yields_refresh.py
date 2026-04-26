"""
Unit tests for the daily ETF-yield refresh job. Mocks TWSE + FinMind +
the Redis cache so the test runs offline. Pure-unit; no aiosqlite/passlib
dependency.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

import tasks.tw_etf_yields_refresh as job
import services.tw_market_service as svc


@pytest.fixture
def fake_exchange_map(monkeypatch):
    monkeypatch.setattr(svc, "_exchange_map", {
        "0050":   "TWSE",
        "00713":  "TWSE",
        "00878":  "TWSE",
        "2330":   "TWSE",  # regular stock — should be ignored
        "1101":   "TWSE",
    })


def _stock_row(code: str, price: float) -> dict:
    return {"Code": code, "ClosingPrice": str(price)}


def _div_row(date_str: str, cash: float, statutory: float = 0.0) -> dict:
    return {
        "date": date_str,
        "CashEarningsDistribution": cash,
        "CashStatutorySurplus": statutory,
        "StockEarningsDistribution": 0,
        "StockStatutorySurplus": 0,
    }


@pytest.mark.asyncio
async def test_refresh_writes_yields_for_etfs_only(fake_exchange_map):
    """Iterates ETFs only, computes TTM yield, writes single Redis key."""
    today_iso = job.date.today().isoformat()
    six_months_ago = (job.date.today() - job.timedelta(days=180)).isoformat()
    two_years_ago = (job.date.today() - job.timedelta(days=730)).isoformat()

    stocks = [
        _stock_row("0050",  150.0),
        _stock_row("00713", 50.0),
        _stock_row("00878", 25.0),
        _stock_row("2330",  900.0),
        _stock_row("1101",  40.0),
    ]
    # 0050 paid 5 + 1.5 = 6.5 in last 12mo → yield = 6.5 / 150 = 4.33%
    # 00713 paid 1.0 + 1.0 = 2.0 → yield = 2.0 / 50 = 4.00%
    # 00878 paid 0.5 in last 12mo + 0.4 stale → yield = 0.5 / 25 = 2.00%
    div_data = {
        "0050":  [_div_row(today_iso, 5.0), _div_row(six_months_ago, 1.5)],
        "00713": [_div_row(today_iso, 1.0), _div_row(six_months_ago, 1.0)],
        "00878": [_div_row(six_months_ago, 0.5), _div_row(two_years_ago, 0.4)],
    }

    captured: dict[str, str] = {}

    async def _div_mock(sym, start_date=None):
        return div_data.get(sym, [])

    async def _set_mock(key, value, ttl):
        captured[key] = value

    with patch.object(job.twse, "get_all_twse_symbols", new_callable=AsyncMock, return_value=stocks), \
         patch.object(job.finmind, "get_dividends", new=_div_mock), \
         patch.object(job, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(job, "cache_set", new=_set_mock):
        await job.refresh_tw_etf_yields()

    assert job.ETF_YIELDS_KEY in captured
    yields = json.loads(captured[job.ETF_YIELDS_KEY])
    assert "2330" not in yields and "1101" not in yields  # regular stocks skipped
    assert yields["0050"] == pytest.approx(4.33, abs=0.01)
    assert yields["00713"] == pytest.approx(4.00, abs=0.01)
    assert yields["00878"] == pytest.approx(2.00, abs=0.01)


@pytest.mark.asyncio
async def test_refresh_skips_when_cached(fake_exchange_map):
    """skip_if_cached=True respects existing cache to save FinMind quota."""
    finmind_mock = AsyncMock(return_value=[])
    cache_set_mock = AsyncMock()
    with patch.object(job, "cache_get", new_callable=AsyncMock, return_value="{}"), \
         patch.object(job.twse, "get_all_twse_symbols", new_callable=AsyncMock) as twse_mock, \
         patch.object(job.finmind, "get_dividends", new=finmind_mock), \
         patch.object(job, "cache_set", new=cache_set_mock):
        await job.refresh_tw_etf_yields(skip_if_cached=True)

    twse_mock.assert_not_called()
    finmind_mock.assert_not_called()
    cache_set_mock.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_skips_etf_with_no_dividends(fake_exchange_map):
    """ETF with FinMind returning [] (no payouts) is omitted from the result."""
    stocks = [_stock_row("0050", 150.0), _stock_row("00713", 50.0)]
    div_data = {
        "0050":  [_div_row(job.date.today().isoformat(), 5.0)],
        "00713": [],
    }
    captured: dict[str, str] = {}

    async def _div_mock(sym, start_date=None):
        return div_data.get(sym, [])

    async def _set_mock(key, value, ttl):
        captured[key] = value

    with patch.object(job.twse, "get_all_twse_symbols", new_callable=AsyncMock, return_value=stocks), \
         patch.object(job.finmind, "get_dividends", new=_div_mock), \
         patch.object(job, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(job, "cache_set", new=_set_mock):
        await job.refresh_tw_etf_yields()

    yields = json.loads(captured[job.ETF_YIELDS_KEY])
    assert "0050" in yields
    assert "00713" not in yields


@pytest.mark.asyncio
async def test_refresh_no_op_when_exchange_map_empty(monkeypatch):
    """Empty _exchange_map (e.g. before symbol-map warmup) skips silently."""
    monkeypatch.setattr(svc, "_exchange_map", {})
    twse_mock = AsyncMock()
    with patch.object(job, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(job.twse, "get_all_twse_symbols", new=twse_mock):
        await job.refresh_tw_etf_yields()

    twse_mock.assert_not_called()


def test_sum_recent_cash_filters_by_cutoff_and_sums_components():
    cutoff = "2024-01-01"
    rows = [
        # In window: cash 2 + 0.5 = 2.5
        {"date": "2024-06-15", "CashEarningsDistribution": 2.0, "CashStatutorySurplus": 0.5},
        # In window: 1 + 0
        {"date": "2024-09-15", "CashEarningsDistribution": 1.0},
        # Out of window
        {"date": "2023-06-15", "CashEarningsDistribution": 5.0},
        # Garbage
        {"date": "2024-12-15", "CashEarningsDistribution": "not a number"},
    ]
    assert job._sum_recent_cash(rows, cutoff) == pytest.approx(3.5)


def test_sum_recent_cash_handles_none_components():
    rows = [
        {"date": "2024-06-15", "CashEarningsDistribution": None, "CashStatutorySurplus": 1.5},
    ]
    assert job._sum_recent_cash(rows, "2024-01-01") == pytest.approx(1.5)


# ── service merges ETF yields ───────────────────────────────────


@pytest.mark.asyncio
async def test_valuations_merge_etf_yields_into_bulk_dict():
    """_get_all_valuations_cached merges ETF yields on top of BWIBBU."""
    bwibbu = {
        "2330": {"pe_ratio": 22.0, "pb_ratio": 6.0, "dividend_yield": 1.6},
    }
    etf_yields = {"0050": 3.5, "00713": 4.7}

    async def _cache_get(key):
        if key == "tw:valuations:all":
            return json.dumps(bwibbu)
        if key == "tw:etf_yields_all":
            return json.dumps(etf_yields)
        return None

    with patch.object(svc, "cache_get", new=_cache_get), \
         patch.object(svc, "cache_set", new_callable=AsyncMock):
        merged = await svc._get_all_valuations_cached()

    assert merged["2330"]["pe_ratio"] == pytest.approx(22.0)
    assert merged["0050"]["dividend_yield"] == pytest.approx(3.5)
    assert merged["0050"]["pe_ratio"] is None
    assert merged["00713"]["dividend_yield"] == pytest.approx(4.7)


@pytest.mark.asyncio
async def test_valuations_merge_handles_empty_etf_cache():
    """Cold ETF cache → BWIBBU returned unmodified."""
    bwibbu = {"2330": {"pe_ratio": 22.0, "pb_ratio": 6.0, "dividend_yield": 1.6}}

    async def _cache_get(key):
        return json.dumps(bwibbu) if key == "tw:valuations:all" else None

    with patch.object(svc, "cache_get", new=_cache_get), \
         patch.object(svc, "cache_set", new_callable=AsyncMock):
        merged = await svc._get_all_valuations_cached()

    assert merged == bwibbu
