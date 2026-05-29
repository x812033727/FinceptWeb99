"""
Service-layer waterfall tests for services.us_market_service.

Connectors (Polygon / yfinance / Stooq / FRED) are individually
covered in test_*_connector.py. This file pins down the *orchestration*
on top of them — the order of try / except branches, what gets cached
(and what deliberately doesn't), and how Polygon-vs-yfinance is
selected based on the API-key config.

Mocking strategy:
  - All three connector modules are patched at the service's
    import-site (svc.polygon, svc.yfinance, svc.stooq) with AsyncMocks.
  - cache_get / cache_set are patched so we don't need a real Redis
    AND so each test can assert on what was (or wasn't) cached.
  - settings.POLYGON_API_KEY is patched per-test to flip the
    Polygon-vs-yfinance path explicitly.
"""
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

import services.us_market_service as svc


# ── Pure helpers ──────────────────────────────────────────────────

def test_period_to_dates_returns_today_and_today_minus_delta():
    """Maps period strings → (start, end) ISO dates. End is always
    today; start is today - delta_map[period] days. Unknown periods
    fall back to 365 days (1y default)."""
    today = date.today()
    start, end = svc._period_to_dates("1y")
    assert end == today.isoformat()
    assert start == (today - timedelta(days=365)).isoformat()


@pytest.mark.parametrize("period,days", [
    ("1d", 1), ("5d", 5), ("1mo", 30), ("3mo", 90),
    ("6mo", 180), ("1y", 365), ("2y", 730), ("5y", 1825), ("10y", 3650),
])
def test_period_to_dates_delta_map(period, days):
    today = date.today()
    start, _ = svc._period_to_dates(period)
    assert start == (today - timedelta(days=days)).isoformat()


def test_period_to_dates_falls_back_to_365_days_for_unknown_period():
    today = date.today()
    start, _ = svc._period_to_dates("not-a-period")
    assert start == (today - timedelta(days=365)).isoformat()


@pytest.mark.parametrize("interval,expected", [
    ("1m", "minute"), ("5m", "minute"), ("15m", "minute"),
    ("1h", "hour"),
    ("1d", "day"), ("1wk", "week"), ("1mo", "month"),
    ("garbage", "day"),  # default
])
def test_interval_to_timespan(interval, expected):
    assert svc._interval_to_timespan(interval) == expected


# ── _normalize_quote ──────────────────────────────────────────────

def test_normalize_quote_fills_required_fields_from_empty_raw():
    """Even when every connector fails (raw == {}), the normalizer
    must still emit a structurally-valid dict so callers / Pydantic
    validators don't blow up."""
    out = svc._normalize_quote("aapl", {})
    assert out["symbol"] == "AAPL"
    assert out["market"] == "US"
    assert out["currency"] == "USD"
    assert out["price"] == 0
    assert out["change"] == 0
    assert out["volume"] == 0
    assert isinstance(out["ts"], int)


def test_normalize_quote_passes_through_raw_fields_when_present():
    raw = {
        "name": "Apple Inc.", "price": 204.5, "change": 4.5,
        "change_pct": 2.25, "volume": 1000, "open": 200,
        "high": 205, "low": 199, "prev_close": 200,
        "market_cap": 3.2e12, "ts": 1700000000000,
    }
    out = svc._normalize_quote("AAPL", raw)
    assert out["price"] == 204.5
    assert out["market_cap"] == 3.2e12
    assert out["ts"] == 1700000000000


# ── _normalize_bars ───────────────────────────────────────────────

def test_normalize_bars_converts_unix_ms_to_iso_date_for_daily_interval():
    """Polygon bars come back with `time` as Unix ms. For daily/weekly/
    monthly the service rewrites that to YYYY-MM-DD so the frontend
    chart's daily-bucket logic works the same regardless of source."""
    bars = [{"time": 1700000000000, "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 1000}]
    out = svc._normalize_bars(bars, "1d")
    assert out[0]["time"] == "2023-11-14"  # 2023-11-14T22:13:20Z


def test_normalize_bars_keeps_unix_ms_for_intraday_intervals():
    bars = [{"time": 1700000000000, "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 1000}]
    out = svc._normalize_bars(bars, "5m")
    assert out[0]["time"] == 1700000000000


def test_normalize_bars_keeps_string_time_unchanged():
    bars = [{"time": "2024-01-02", "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 1000}]
    out = svc._normalize_bars(bars, "1d")
    assert out[0]["time"] == "2024-01-02"


# ── get_quote waterfall ───────────────────────────────────────────

def _patch_quote_chain(*, polygon_quote=None, yfinance_quote=None, stooq_quote=None,
                      polygon_key="key", cache_value=None):
    """Helper context manager: assemble the full set of patches needed
    for a get_quote test. Each connector method is an AsyncMock (or
    side_effect-raising mock if the value is an Exception)."""
    def _async_or_raise(value):
        if isinstance(value, Exception):
            return AsyncMock(side_effect=value)
        return AsyncMock(return_value=value if value is not None else {})

    patches = [
        patch.object(svc, "cache_get_json", new=AsyncMock(return_value=cache_value)),
        patch.object(svc, "cache_set_json", new_callable=AsyncMock),
        patch.object(svc.settings, "POLYGON_API_KEY", polygon_key),
        patch.object(svc.polygon, "get_quote", new=_async_or_raise(polygon_quote)),
        patch.object(svc.yfinance, "get_quote", new=_async_or_raise(yfinance_quote)),
        patch.object(svc.stooq, "get_quote", new=_async_or_raise(stooq_quote)),
    ]
    return patches


@pytest.mark.asyncio
async def test_get_quote_short_circuits_on_cache_hit_without_calling_connectors():
    cached = {"symbol": "AAPL", "price": 200, "market": "US", "currency": "USD"}
    patches = _patch_quote_chain(cache_value=cached, polygon_quote={"price": 999})
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5] as stooq_mock:
        # Need to inspect Polygon mock too — re-grab via context.
        pass

    # Cleaner: open the patches in a single with-block.
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=cached)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock) as cache_set, \
         patch.object(svc.settings, "POLYGON_API_KEY", "key"), \
         patch.object(svc.polygon, "get_quote", new_callable=AsyncMock) as polygon_mock, \
         patch.object(svc.yfinance, "get_quote", new_callable=AsyncMock) as yf_mock, \
         patch.object(svc.stooq, "get_quote", new_callable=AsyncMock) as stooq_mock:
        out = await svc.get_quote("AAPL")

    assert out["price"] == 200
    polygon_mock.assert_not_called()
    yf_mock.assert_not_called()
    stooq_mock.assert_not_called()
    cache_set.assert_not_called()  # already cached, no rewrite


@pytest.mark.asyncio
async def test_get_quote_uses_polygon_when_key_set():
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.settings, "POLYGON_API_KEY", "key"), \
         patch.object(svc.polygon, "get_quote", new=AsyncMock(return_value={"price": 204.5})), \
         patch.object(svc.yfinance, "get_quote", new_callable=AsyncMock) as yf_mock, \
         patch.object(svc.stooq, "get_quote", new_callable=AsyncMock) as stooq_mock:
        out = await svc.get_quote("AAPL")

    assert out["price"] == 204.5
    yf_mock.assert_not_called()
    stooq_mock.assert_not_called()


@pytest.mark.asyncio
async def test_get_quote_uses_yfinance_when_polygon_key_missing():
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc.polygon, "get_quote", new_callable=AsyncMock) as polygon_mock, \
         patch.object(svc.yfinance, "get_quote", new=AsyncMock(return_value={"price": 200})), \
         patch.object(svc.stooq, "get_quote", new_callable=AsyncMock):
        out = await svc.get_quote("AAPL")

    assert out["price"] == 200
    polygon_mock.assert_not_called()


@pytest.mark.asyncio
async def test_get_quote_falls_back_to_yfinance_when_polygon_raises():
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.settings, "POLYGON_API_KEY", "key"), \
         patch.object(svc.polygon, "get_quote", new=AsyncMock(side_effect=RuntimeError("polygon down"))), \
         patch.object(svc.yfinance, "get_quote", new=AsyncMock(return_value={"price": 200})), \
         patch.object(svc.stooq, "get_quote", new_callable=AsyncMock):
        out = await svc.get_quote("AAPL")
    assert out["price"] == 200


@pytest.mark.asyncio
async def test_get_quote_falls_back_to_stooq_when_polygon_and_yfinance_yield_no_price():
    """raw.get('price') is falsy (0 or missing) → Stooq tier triggers."""
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.settings, "POLYGON_API_KEY", "key"), \
         patch.object(svc.polygon, "get_quote", new=AsyncMock(return_value={"price": 0})), \
         patch.object(svc.yfinance, "get_quote", new_callable=AsyncMock), \
         patch.object(svc.stooq, "get_quote", new=AsyncMock(return_value={"price": 198, "volume": 500})):
        out = await svc.get_quote("AAPL")
    assert out["price"] == 198
    assert out["volume"] == 500


@pytest.mark.asyncio
async def test_get_quote_returns_zero_dict_when_every_tier_fails():
    """All three tiers down → connector returns a zero-price dict that
    is *deliberately not cached* (would otherwise lock in the failure
    for TTL_QUOTE seconds)."""
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock) as cache_set, \
         patch.object(svc.settings, "POLYGON_API_KEY", "key"), \
         patch.object(svc.polygon, "get_quote", new=AsyncMock(side_effect=RuntimeError("p"))), \
         patch.object(svc.yfinance, "get_quote", new=AsyncMock(side_effect=RuntimeError("y"))), \
         patch.object(svc.stooq, "get_quote", new=AsyncMock(side_effect=RuntimeError("s"))):
        out = await svc.get_quote("AAPL")

    assert out["symbol"] == "AAPL"
    assert out["price"] == 0
    cache_set.assert_not_called()  # critical — don't lock in zero state


@pytest.mark.asyncio
async def test_get_quote_caches_only_when_price_is_nonzero():
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock) as cache_set, \
         patch.object(svc.settings, "POLYGON_API_KEY", "key"), \
         patch.object(svc.polygon, "get_quote", new=AsyncMock(return_value={"price": 100})), \
         patch.object(svc.yfinance, "get_quote", new_callable=AsyncMock), \
         patch.object(svc.stooq, "get_quote", new_callable=AsyncMock):
        await svc.get_quote("AAPL")

    cache_set.assert_called_once()
    # Cache key + TTL.
    args, _ = cache_set.call_args
    assert args[0].startswith("us:quote:")
    assert args[2] == svc.TTL_QUOTE


# ── get_history waterfall ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_history_uses_polygon_with_date_range_when_key_set():
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.settings, "POLYGON_API_KEY", "key"), \
         patch.object(svc.polygon, "get_aggs", new=AsyncMock(return_value=[
             {"time": 1700000000000, "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 1000}
         ])) as polygon_mock, \
         patch.object(svc.yfinance, "get_history", new_callable=AsyncMock) as yf_mock:
        out = await svc.get_history("AAPL", period="1mo", interval="1d")

    polygon_mock.assert_called_once()
    yf_mock.assert_not_called()
    # Daily interval normalises Unix ms to ISO date.
    assert out[0]["time"] == "2023-11-14"


@pytest.mark.asyncio
async def test_get_history_falls_back_to_yfinance_when_polygon_raises():
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.settings, "POLYGON_API_KEY", "key"), \
         patch.object(svc.polygon, "get_aggs", new=AsyncMock(side_effect=RuntimeError("polygon"))), \
         patch.object(svc.yfinance, "get_history", new=AsyncMock(return_value=[
             {"time": 1700000000000, "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 1000}
         ])):
        out = await svc.get_history("AAPL")

    assert len(out) == 1


@pytest.mark.asyncio
async def test_get_history_uses_yfinance_directly_when_no_polygon_key():
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc.polygon, "get_aggs", new_callable=AsyncMock) as polygon_mock, \
         patch.object(svc.yfinance, "get_history", new=AsyncMock(return_value=[])):
        await svc.get_history("AAPL")
    polygon_mock.assert_not_called()


@pytest.mark.asyncio
async def test_get_history_short_circuits_on_cache_hit():
    cached = [{"time": "2024-01-01", "open": 1, "high": 2, "low": 0, "close": 1, "volume": 100}]
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=cached)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock) as cache_set, \
         patch.object(svc.polygon, "get_aggs", new_callable=AsyncMock) as polygon_mock, \
         patch.object(svc.yfinance, "get_history", new_callable=AsyncMock) as yf_mock:
        out = await svc.get_history("AAPL")

    assert len(out) == 1
    polygon_mock.assert_not_called()
    yf_mock.assert_not_called()
    cache_set.assert_not_called()


# ── get_fundamentals waterfall ────────────────────────────────────

@pytest.mark.asyncio
async def test_get_fundamentals_uses_polygon_when_key_set():
    polygon_payload = {
        "name": "Apple Inc.", "sic_description": "Tech",
        "market_cap": 3.2e12, "description": "iPhone maker",
    }
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.settings, "POLYGON_API_KEY", "key"), \
         patch.object(svc.polygon, "get_ticker_details", new=AsyncMock(return_value=polygon_payload)), \
         patch.object(svc.yfinance, "get_info", new_callable=AsyncMock) as yf_mock:
        out = await svc.get_fundamentals("AAPL")

    yf_mock.assert_not_called()
    assert out["symbol"] == "AAPL"
    assert out["name"] == "Apple Inc."
    assert out["sector"] == "Tech"
    assert out["market_cap"] == 3.2e12


@pytest.mark.asyncio
async def test_get_fundamentals_falls_back_to_yfinance_when_no_polygon_key():
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc.polygon, "get_ticker_details", new_callable=AsyncMock) as polygon_mock, \
         patch.object(svc.yfinance, "get_info", new=AsyncMock(return_value={
             "longName": "Apple Inc.", "trailingPE": 30.5,
             "marketCap": 3.2e12, "sector": "Technology",
         })):
        out = await svc.get_fundamentals("AAPL")

    polygon_mock.assert_not_called()
    assert out["pe_ratio"] == 30.5
    assert out["sector"] == "Technology"


@pytest.mark.asyncio
async def test_get_fundamentals_falls_back_to_yfinance_when_polygon_raises():
    """Polygon outage / 429 / network error → yfinance."""
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.settings, "POLYGON_API_KEY", "key"), \
         patch.object(svc.polygon, "get_ticker_details", new=AsyncMock(side_effect=RuntimeError("429"))), \
         patch.object(svc.yfinance, "get_info", new=AsyncMock(return_value={
             "longName": "Apple Inc.", "trailingPE": 30,
         })):
        out = await svc.get_fundamentals("AAPL")
    assert out["pe_ratio"] == 30


@pytest.mark.asyncio
async def test_get_fundamentals_short_circuits_on_cache_hit():
    cached = {"symbol": "AAPL", "market": "US", "name": "Apple Inc.", "fetched_at": "2024-01-01"}
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=cached)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock) as cache_set, \
         patch.object(svc.polygon, "get_ticker_details", new_callable=AsyncMock) as polygon_mock, \
         patch.object(svc.yfinance, "get_info", new_callable=AsyncMock) as yf_mock:
        out = await svc.get_fundamentals("AAPL")
    assert out["name"] == "Apple Inc."
    polygon_mock.assert_not_called()
    yf_mock.assert_not_called()
    cache_set.assert_not_called()
