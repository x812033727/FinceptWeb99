"""
Service-layer waterfall tests for services.tw_market_service.

Mirrors test_us_market_service_waterfall.py for the TW data path:

  Quote          : TWSE realtime → FinMind 7-day fallback
  History        : TWSE month-by-month → FinMind range fallback
  Institutional  : FinMind range → TWSE today fallback
  Margin         : FinMind range → TWSE today fallback
  Revenue        : FinMind monthly → MOPS HTML fallback
  Fundamentals   : TWSE BWIBBU_d (no fallback — TW PE/PB/yield is
                   only published by TWSE)

The connector layers (TWSE / FinMind / MOPS) have their own unit
tests — this file pins down the orchestration: try-except chain
order, which fallback runs when the primary errors vs returns empty,
and the consistent "don't cache zero/empty results" rule.
"""
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

import services.tw_market_service as svc


# ── Pure helpers ──────────────────────────────────────────────────

@pytest.mark.parametrize("sym,expected", [
    ("0050",   True),   # 5-digit ETF
    ("0056",   True),
    ("00713",  True),   # 5-digit ETF
    ("006208", True),   # 6-digit ETF
    ("00637L", True),   # leveraged
    ("00640R", True),   # inverse
    ("2330",   False),  # 4-digit stock
    ("9999",   False),
    ("",       False),
    ("ABCD",   False),
])
def test_is_etf_matches_tw_etf_code_pattern(sym, expected):
    assert svc.is_etf(sym) is expected


def test_today_str_returns_iso_today():
    assert svc._today_str() == date.today().isoformat()


def test_start_date_subtracts_months_in_30_day_chunks():
    # 6 months → ~180 days back. Use approximate compare so the test
    # doesn't fail at midnight UTC vs local.
    out = svc._start_date(months=6)
    delta = (date.today() - date.fromisoformat(out)).days
    assert 178 <= delta <= 182


def test_get_exchange_returns_twse_for_unknown_symbol():
    """Default for any symbol not in the runtime-built _exchange_map.
    refresh_symbol_map() populates it daily; tests start with empty."""
    assert svc.get_exchange("9999") == "TWSE"


# ── _normalize_quote ──────────────────────────────────────────────

def test_normalize_quote_emits_required_fields_from_empty_raw():
    out = svc._normalize_quote("2330", {})
    assert out["symbol"] == "2330"
    assert out["market"] == "TW"
    assert out["currency"] == "TWD"
    assert out["price"] == 0
    assert out["volume"] == 0
    assert out["tz"] == "Asia/Taipei"


def test_normalize_quote_computes_change_and_change_pct_when_close_and_prev_present():
    out = svc._normalize_quote("2330", {"close": 785, "prev_close": 780})
    # change derived from close-prev when raw doesn't give us one
    assert out["change"] == 5
    assert out["change_pct"] == round(5 / 780 * 100, 4)


def test_normalize_quote_uses_explicit_change_when_provided():
    """If TWSE supplies a precomputed change, the connector trusts it
    rather than rederiving from prev_close (which may be stale)."""
    out = svc._normalize_quote("2330", {"close": 785, "prev_close": 780, "change": 4.5})
    assert out["change"] == 4.5  # not 5


def test_normalize_quote_change_pct_is_none_when_prev_close_missing():
    out = svc._normalize_quote("2330", {"close": 785})
    assert out["change_pct"] is None


def test_normalize_quote_derives_prev_close_when_only_change_present():
    """Regression: TWSE realtime returns `change` but no `prev_close`.
    Before this fix change_pct fell out as None and the watchlist UI
    showed a blank 漲跌 column. Now we derive prev_close = close -
    change so change_pct can be computed."""
    out = svc._normalize_quote("2330", {"close": 785, "change": 5})
    assert out["change"] == 5
    # prev_close should be 780 (785 - 5), so change_pct = 5/780*100
    assert out["change_pct"] == round(5 / 780 * 100, 4)


def test_normalize_quote_handles_negative_change_with_only_change_present():
    out = svc._normalize_quote("2330", {"close": 600, "change": -10})
    assert out["change"] == -10
    # prev_close = 610, change_pct = -10/610*100
    assert out["change_pct"] == round(-10 / 610 * 100, 4)


def test_normalize_quote_marks_etf_correctly():
    assert svc._normalize_quote("0050", {})["is_etf"] is True
    assert svc._normalize_quote("2330", {})["is_etf"] is False


# ── get_quote waterfall ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_quote_short_circuits_on_cache_hit():
    cached = '{"symbol":"2330","price":785,"market":"TW"}'
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=cached)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock) as cache_set, \
         patch.object(svc.twse, "get_realtime_quote", new_callable=AsyncMock) as twse_mock, \
         patch.object(svc.finmind, "get_daily_ohlcv", new_callable=AsyncMock) as finmind_mock:
        out = await svc.get_quote("2330")

    assert out["price"] == 785
    twse_mock.assert_not_called()
    finmind_mock.assert_not_called()
    cache_set.assert_not_called()


@pytest.mark.asyncio
async def test_get_quote_uses_twse_realtime_when_available():
    twse_payload = {"symbol": "2330", "name_zh": "台積電",
                    "close": 785, "open": 780, "high": 790, "low": 775, "volume": 1000}
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_realtime_quote", new=AsyncMock(return_value=twse_payload)), \
         patch.object(svc.finmind, "get_daily_ohlcv", new_callable=AsyncMock) as finmind_mock:
        out = await svc.get_quote("2330")

    assert out["price"] == 785
    assert out["name_zh"] == "台積電"
    finmind_mock.assert_not_called()


@pytest.mark.asyncio
async def test_get_quote_falls_back_to_finmind_when_twse_returns_none():
    """TWSE returns None when the symbol isn't in today's STOCK_DAY_ALL
    feed — connector's `if not raw:` triggers FinMind fallback."""
    finmind_bars = [
        {"time": "2024-04-01", "open": 780, "high": 790, "low": 775,
         "close": 785, "volume": 1000},
    ]
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_realtime_quote", new=AsyncMock(return_value=None)), \
         patch.object(svc.finmind, "get_daily_ohlcv", new=AsyncMock(return_value=finmind_bars)):
        out = await svc.get_quote("2330")

    assert out["price"] == 785
    assert out["volume"] == 1000


@pytest.mark.asyncio
async def test_get_quote_falls_back_to_finmind_when_twse_raises():
    finmind_bars = [{"time": "2024-04-01", "open": 1, "high": 2, "low": 0,
                     "close": 1.5, "volume": 100}]
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_realtime_quote", new=AsyncMock(side_effect=RuntimeError("twse"))), \
         patch.object(svc.finmind, "get_daily_ohlcv", new=AsyncMock(return_value=finmind_bars)):
        out = await svc.get_quote("2330")
    assert out["price"] == 1.5


@pytest.mark.asyncio
async def test_finmind_fallback_computes_change_pct_from_prev_bar():
    """Regression: FinMind EOD fallback used to set change=None and
    omit prev_close, so the watchlist showed a blank 漲跌 column off-
    hours. Now we read bars[-2].close as prev_close and compute the
    delta — change_pct must come back populated."""
    finmind_bars = [
        {"time": "2024-04-01", "open": 778, "high": 782, "low": 776,
         "close": 780, "volume": 800},
        {"time": "2024-04-02", "open": 781, "high": 790, "low": 779,
         "close": 785, "volume": 1000},
    ]
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_realtime_quote",
                      new=AsyncMock(return_value=None)), \
         patch.object(svc.finmind, "get_daily_ohlcv",
                      new=AsyncMock(return_value=finmind_bars)):
        out = await svc.get_quote("2330")

    assert out["price"] == 785
    assert out["change"] == 5  # 785 - 780
    assert out["change_pct"] == round(5 / 780 * 100, 4)


@pytest.mark.asyncio
async def test_finmind_fallback_skips_change_pct_with_only_one_bar():
    """Single FinMind bar — no prior day to subtract from. change /
    change_pct stay None rather than crashing."""
    finmind_bars = [
        {"time": "2024-04-02", "open": 781, "high": 790, "low": 779,
         "close": 785, "volume": 1000},
    ]
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_realtime_quote",
                      new=AsyncMock(return_value=None)), \
         patch.object(svc.finmind, "get_daily_ohlcv",
                      new=AsyncMock(return_value=finmind_bars)):
        out = await svc.get_quote("2330")

    assert out["price"] == 785
    assert out["change"] is None
    assert out["change_pct"] is None


@pytest.mark.asyncio
async def test_get_quote_returns_zero_dict_when_both_tiers_fail_and_does_not_cache():
    """TWSE + FinMind both blow up → connector still returns a
    structurally-valid dict with price=0, but MUST NOT cache it."""
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock) as cache_set, \
         patch.object(svc.twse, "get_realtime_quote", new=AsyncMock(side_effect=RuntimeError("t"))), \
         patch.object(svc.finmind, "get_daily_ohlcv", new=AsyncMock(side_effect=RuntimeError("f"))):
        out = await svc.get_quote("2330")

    assert out["symbol"] == "2330"
    assert out["price"] == 0
    assert out["data_source"] == "unavailable"
    cache_set.assert_not_called()


@pytest.mark.asyncio
async def test_get_quote_marks_data_source_twse_when_realtime_serves():
    twse_payload = {"symbol": "2330", "name_zh": "台積電", "close": 785,
                    "open": 780, "high": 790, "low": 775, "volume": 1000}
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_realtime_quote", new=AsyncMock(return_value=twse_payload)):
        out = await svc.get_quote("2330")
    assert out["data_source"] == "twse"


@pytest.mark.asyncio
async def test_get_quote_marks_data_source_finmind_when_finmind_serves():
    finmind_bars = [{"time": "2024-04-01", "open": 1, "high": 2, "low": 0,
                     "close": 1.5, "volume": 100}]
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_realtime_quote", new=AsyncMock(return_value=None)), \
         patch.object(svc.finmind, "get_daily_ohlcv", new=AsyncMock(return_value=finmind_bars)):
        out = await svc.get_quote("2330")
    assert out["data_source"] == "finmind"
    assert out["price"] == 1.5


# ── get_history waterfall ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_history_uses_twse_when_available():
    """TWSE returns one month at a time; service walks back N months
    and concatenates. Test with months=2 so the loop iterates twice."""
    month_bars = [{"time": "2024-04-01", "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 100}]
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_daily_ohlcv", new=AsyncMock(return_value=month_bars)) as twse_mock, \
         patch.object(svc.finmind, "get_daily_ohlcv", new_callable=AsyncMock) as finmind_mock:
        out = await svc.get_history("2330", months=2)

    assert twse_mock.await_count == 2  # one call per month
    finmind_mock.assert_not_called()
    assert len(out) == 2  # one bar per month, concatenated


@pytest.mark.asyncio
async def test_get_history_falls_back_to_finmind_when_twse_raises():
    finmind_bars = [{"time": "2024-04-01", "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 100}]
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_daily_ohlcv", new=AsyncMock(side_effect=RuntimeError("twse"))), \
         patch.object(svc.finmind, "get_daily_ohlcv", new=AsyncMock(return_value=finmind_bars)):
        out = await svc.get_history("2330")
    assert len(out) == 1


@pytest.mark.asyncio
async def test_get_history_does_not_cache_when_both_tiers_empty():
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock) as cache_set, \
         patch.object(svc.twse, "get_daily_ohlcv", new=AsyncMock(return_value=[])), \
         patch.object(svc.finmind, "get_daily_ohlcv", new=AsyncMock(return_value=[])):
        out = await svc.get_history("2330")

    assert out == []
    cache_set.assert_not_called()


# ── get_institutional waterfall ──────────────────────────────────

@pytest.mark.asyncio
async def test_get_institutional_uses_finmind_first_with_date_range():
    """FinMind goes first because it returns per-symbol range; TWSE
    would require N day-by-day calls."""
    finmind_rows = [{"date": "2024-04-01", "fini_buy": 1000, "fini_sell": 500}]
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc.finmind, "get_institutional", new=AsyncMock(return_value=finmind_rows)), \
         patch.object(svc.twse, "get_institutional", new_callable=AsyncMock) as twse_mock:
        out = await svc.get_institutional("2330")

    assert out == finmind_rows
    twse_mock.assert_not_called()


@pytest.mark.asyncio
async def test_get_institutional_falls_back_to_twse_today_when_finmind_empty():
    """FinMind quota exhausted → []; TWSE returns a full-market list
    that the service filters down to the requested symbol."""
    twse_rows = [
        {"symbol": "2330", "fini_buy": 1000},
        {"symbol": "2317", "fini_buy": 500},
    ]
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc.finmind, "get_institutional", new=AsyncMock(return_value=[])), \
         patch.object(svc.twse, "get_institutional", new=AsyncMock(return_value=twse_rows)):
        out = await svc.get_institutional("2330")

    # Filtered to only the asked symbol.
    assert len(out) == 1
    assert out[0]["symbol"] == "2330"


# ── get_margin waterfall ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_margin_falls_back_to_twse_filtered_by_symbol():
    twse_rows = [
        {"symbol": "2330", "margin_purchase": 1000},
        {"symbol": "2317", "margin_purchase": 200},
    ]
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc.finmind, "get_margin", new=AsyncMock(return_value=[])), \
         patch.object(svc.twse, "get_margin", new=AsyncMock(return_value=twse_rows)):
        out = await svc.get_margin("2330")

    assert len(out) == 1
    assert out[0]["symbol"] == "2330"


# ── get_revenue waterfall ────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_revenue_uses_finmind_when_available():
    finmind_rows = [{"date": "2024-04-01", "revenue": 100_000_000}]
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc.finmind, "get_monthly_revenue", new=AsyncMock(return_value=finmind_rows)), \
         patch.object(svc.mops, "get_monthly_revenue", new_callable=AsyncMock) as mops_mock:
        out = await svc.get_revenue("2330")

    assert out == finmind_rows
    mops_mock.assert_not_called()


@pytest.mark.asyncio
async def test_get_revenue_falls_back_to_mops_html_scrape_when_finmind_fails():
    mops_rows = [{"symbol": "2330", "date": "2024-04-01", "revenue": 100_000_000, "source": "mops"}]
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc.finmind, "get_monthly_revenue", new=AsyncMock(side_effect=RuntimeError("quota"))), \
         patch.object(svc.mops, "get_monthly_revenue", new=AsyncMock(return_value=mops_rows)):
        out = await svc.get_revenue("2330")
    assert out == mops_rows


# ── get_fundamentals (TWSE only — no fallback) ───────────────────

@pytest.mark.asyncio
async def test_get_fundamentals_returns_minimal_shell_when_twse_empty():
    """No PE/PB/yield → connector still returns the minimal shell
    (symbol/market/exchange) so the frontend can render the page,
    but does NOT cache (next request retries instead of locking
    blanks for 24h)."""
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock) as cache_set, \
         patch.object(svc.twse, "get_valuation_ratios", new=AsyncMock(return_value={})):
        out = await svc.get_fundamentals("2330")

    assert out["symbol"] == "2330"
    assert out["market"] == "TW"
    assert "pe_ratio" not in out  # no ratios, no field
    cache_set.assert_not_called()


@pytest.mark.asyncio
async def test_get_fundamentals_caches_when_ratios_present():
    ratios = {"pe_ratio": 21.0, "pb_ratio": 5.3, "dividend_yield": 2.6,
              "fetched_at": "2024-04-01"}
    with patch.object(svc, "cache_get", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new_callable=AsyncMock) as cache_set, \
         patch.object(svc.twse, "get_valuation_ratios", new=AsyncMock(return_value=ratios)):
        out = await svc.get_fundamentals("2330")

    assert out["pe_ratio"] == 21.0
    cache_set.assert_called_once()
