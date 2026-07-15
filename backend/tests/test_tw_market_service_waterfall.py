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
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

import services.tw_market_service as svc


@pytest.fixture(autouse=True)
def _isolate_live_mis_tier():
    """Never let orchestration tests hit the live intraday MIS endpoint.

    These tests mock the documented TWSE/FinMind seams. Since MIS became
    Tier 0 during regular hours, an unmocked 09:00-13:30 test run could
    return the real quote and make expectations depend on wall-clock time.
    Dedicated connector tests cover MIS itself; this file stays deterministic.
    """
    with patch.object(
        svc.twse_mis, "get_realtime_quote", new=AsyncMock(return_value=None),
    ):
        yield


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


def test_normalize_quote_prefers_prev_close_over_upstream_change():
    """When prev_close is reliable (set directly OR backfilled from
    the OHLCV archive in fetch_quote_waterfall), close-derived
    change wins over upstream's `change` field. TWSE puts the
    prior-day close in the `Change` slot for some KY-listed stocks
    (4958, 2455 …) — trusting it surfaces +992% headlines.
    Archive-derived prev_close + arithmetic always agrees with the
    candlestick chart."""
    out = svc._normalize_quote("2330", {"close": 785, "prev_close": 780, "change": 4.5})
    assert out["change"] == 5  # 785 - 780, NOT the upstream's 4.5
    assert out["change_pct"] == round(5 / 780 * 100, 4)


def test_normalize_quote_change_pct_is_none_when_prev_close_missing():
    out = svc._normalize_quote("2330", {"close": 785})
    assert out["change_pct"] is None


def test_normalize_quote_drops_change_when_only_upstream_change_present():
    """When prev_close is missing, NEVER derive it from the upstream's
    `change` field. TWSE's STOCK_DAY_ALL puts the prior-day close in
    the `Change` slot for some KY-listed stocks (4958, 2455, …) — the
    old derive-prev-from-change fallback turned that into +992% / +996%
    headlines. Better to surface a blank delta than misleading garbage;
    the prev_close fallback (FinMind) lives in `_resolve_prev_close`,
    not in this normalizer.
    """
    out = svc._normalize_quote("2330", {"close": 785, "change": 5})
    assert out["change"] is None
    assert out["change_pct"] is None
    # And on the downside path:
    out2 = svc._normalize_quote("2330", {"close": 600, "change": -10})
    assert out2["change"] is None
    assert out2["change_pct"] is None


def test_normalize_quote_drops_change_pct_when_implausibly_large():
    """Regression for the 4958 (KY) case where TWSE returned the
    prior-day close in the `Change` field, computing change_pct =
    +992%. Anything beyond ±30% is treated as upstream junk and
    dropped — UI shows '—' until the next refresh instead of a
    misleading headline."""
    # close=421, change=382.5 → derived prev=38.5 → +993% → drop.
    out = svc._normalize_quote("4958", {"close": 421, "change": 382.5})
    assert out["change"] is None
    assert out["change_pct"] is None


def test_normalize_quote_keeps_legal_limit_up():
    """A real ±10% limit-up move stays within the sanity bound and
    must NOT be dropped."""
    # prev_close=100, close=110 → +10/100 = +10%
    out = svc._normalize_quote("2330", {"close": 110, "prev_close": 100})
    assert out["change"] == 10
    assert out["change_pct"] == round(10 / 100 * 100, 4)


def test_normalize_quote_drops_negative_implausible_change():
    """Same bound on the downside — a -50% computed pct is upstream
    junk (TW limit-down is -10%)."""
    # close=10, change=-50 → derived prev=60 → -83% → drop
    out = svc._normalize_quote("4958", {"close": 10, "change": -50})
    assert out["change"] is None
    assert out["change_pct"] is None


def test_normalize_quote_marks_etf_correctly():
    assert svc._normalize_quote("0050", {})["is_etf"] is True
    assert svc._normalize_quote("2330", {})["is_etf"] is False


# ── get_quote waterfall ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_quote_short_circuits_on_cache_hit():
    cached = {"symbol": "2330", "price": 785, "market": "TW"}
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=cached)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock) as cache_set, \
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
    # FinMind may be reached as a prev_close fallback when the archive
    # has nothing for the symbol — that's expected behaviour now (the
    # test DB is empty). Stub it with a single-bar response so prev_close
    # gets populated; assertion below checks TWSE was the QUOTE source.
    finmind_bars = [
        {"date": (date.today() - timedelta(days=1)).isoformat(),
         "open": 780, "high": 790, "low": 775, "close": 780, "volume": 1000},
    ]
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_realtime_quote", new=AsyncMock(return_value=twse_payload)), \
         patch.object(svc.finmind, "get_daily_ohlcv",
                      new=AsyncMock(return_value=finmind_bars)):
        out = await svc.get_quote("2330")

    assert out["price"] == 785
    assert out["name_zh"] == "台積電"
    assert out["data_source"] == "twse"


@pytest.mark.asyncio
async def test_get_quote_falls_back_to_finmind_when_twse_returns_none():
    """TWSE returns None when the symbol isn't in today's STOCK_DAY_ALL
    feed — connector's `if not raw:` triggers FinMind fallback."""
    finmind_bars = [
        {"time": "2024-04-01", "open": 780, "high": 790, "low": 775,
         "close": 785, "volume": 1000},
    ]
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_realtime_quote", new=AsyncMock(return_value=None)), \
         patch.object(svc.finmind, "get_daily_ohlcv", new=AsyncMock(return_value=finmind_bars)):
        out = await svc.get_quote("2330")

    assert out["price"] == 785
    assert out["volume"] == 1000


@pytest.mark.asyncio
async def test_get_quote_falls_back_to_finmind_when_twse_raises():
    finmind_bars = [{"time": "2024-04-01", "open": 1, "high": 2, "low": 0,
                     "close": 1.5, "volume": 100}]
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
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
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
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
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
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
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock) as cache_set, \
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
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_realtime_quote", new=AsyncMock(return_value=twse_payload)):
        out = await svc.get_quote("2330")
    assert out["data_source"] == "twse"


@pytest.mark.asyncio
async def test_get_quote_marks_data_source_finmind_when_finmind_serves():
    finmind_bars = [{"time": "2024-04-01", "open": 1, "high": 2, "low": 0,
                     "close": 1.5, "volume": 100}]
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
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
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_daily_ohlcv", new=AsyncMock(return_value=month_bars)) as twse_mock, \
         patch.object(svc.finmind, "get_daily_ohlcv", new_callable=AsyncMock) as finmind_mock:
        out = await svc.get_history("2330", months=2)

    assert twse_mock.await_count == 2  # one call per month
    finmind_mock.assert_not_called()
    assert len(out) == 2  # one bar per month, concatenated


@pytest.mark.asyncio
async def test_get_history_falls_back_to_finmind_when_twse_raises():
    finmind_bars = [{"time": "2024-04-01", "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 100}]
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_daily_ohlcv", new=AsyncMock(side_effect=RuntimeError("twse"))), \
         patch.object(svc.finmind, "get_daily_ohlcv", new=AsyncMock(return_value=finmind_bars)):
        out = await svc.get_history("2330")
    assert len(out) == 1


@pytest.mark.asyncio
async def test_get_history_does_not_cache_when_both_tiers_empty():
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock) as cache_set, \
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
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.finmind, "get_institutional", new=AsyncMock(return_value=finmind_rows)), \
         patch.object(svc.twse, "get_institutional", new_callable=AsyncMock) as twse_mock:
        out = await svc.get_institutional("2330")

    assert [{k: v for k, v in row.items() if k != "data_source"} for row in out] == finmind_rows
    assert out[0]["data_source"] == "finmind"
    twse_mock.assert_not_called()


@pytest.mark.asyncio
async def test_get_institutional_falls_back_to_twse_today_when_finmind_empty():
    """FinMind quota exhausted → []; TWSE returns a full-market list
    that the service filters down to the requested symbol."""
    twse_rows = [
        {"symbol": "2330", "fini_buy": 1000},
        {"symbol": "2317", "fini_buy": 500},
    ]
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
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
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.finmind, "get_margin", new=AsyncMock(return_value=[])), \
         patch.object(svc.twse, "get_margin", new=AsyncMock(return_value=twse_rows)):
        out = await svc.get_margin("2330")

    assert len(out) == 1
    assert out[0]["symbol"] == "2330"


# ── get_revenue waterfall ────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_revenue_uses_finmind_when_available():
    finmind_rows = [{"date": "2024-04-01", "revenue": 100_000_000}]
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.finmind, "get_monthly_revenue", new=AsyncMock(return_value=finmind_rows)), \
         patch.object(svc.mops, "get_monthly_revenue_recent", new_callable=AsyncMock) as mops_mock:
        out = await svc.get_revenue("2330")

    assert out[0]["date"] == finmind_rows[0]["date"]
    assert out[0]["revenue"] == finmind_rows[0]["revenue"]
    assert out[0]["data_source"] == "finmind"
    mops_mock.assert_not_called()


@pytest.mark.asyncio
async def test_get_revenue_falls_back_to_mops_html_scrape_when_finmind_fails():
    mops_rows = [{"symbol": "2330", "date": "2024-04-01", "revenue": 100_000_000, "source": "mops"}]
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.finmind, "get_monthly_revenue", new=AsyncMock(side_effect=RuntimeError("quota"))), \
         patch.object(svc.mops, "get_monthly_revenue_recent", new=AsyncMock(return_value=mops_rows)):
        out = await svc.get_revenue("2330")
    assert out[0]["symbol"] == mops_rows[0]["symbol"]
    assert out[0]["revenue"] == mops_rows[0]["revenue"]
    assert out[0]["data_source"] == "mops"


# ── get_fundamentals (TWSE only — no fallback) ───────────────────

@pytest.mark.asyncio
async def test_get_fundamentals_returns_minimal_shell_when_twse_empty():
    """No PE/PB/yield → connector still returns the minimal shell
    (symbol/market/exchange) so the frontend can render the page,
    but does NOT cache (next request retries instead of locking
    blanks for 24h)."""
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock) as cache_set, \
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
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock) as cache_set, \
         patch.object(svc.twse, "get_valuation_ratios", new=AsyncMock(return_value=ratios)):
        out = await svc.get_fundamentals("2330")

    assert out["pe_ratio"] == 21.0
    cache_set.assert_called_once()


# ── DB snapshot tier sanity ───────────────────────────────────────


@pytest.mark.asyncio
async def test_db_snapshot_tier_drops_stale_implausible_change_pct():
    """Regression: rows in `quote_snapshots` written before the sanity
    bound (PR #142) carry +992% / +996% values from when the upstream
    bug was firing. The DB-tier fallback used to read those rows
    verbatim and serve the junk back to the UI whenever upstream
    was briefly unreachable. Now the snapshot's stored `change_pct`
    is re-sanitised — implausible values become None."""
    bad_snap = {
        "price": 347.5,
        "volume": 24_628_405,
        "prev_close": None,
        "data_source": "twse",
        "change_pct": 996.84,   # legacy junk
    }
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_realtime_quote",
                      new=AsyncMock(return_value=None)), \
         patch.object(svc.finmind, "get_daily_ohlcv",
                      new=AsyncMock(return_value=[])), \
         patch(
             "services.ingest.repository.read_latest_quote_autosession",
             new=AsyncMock(return_value=bad_snap),
         ):
        out = await svc.get_quote("2455")

    assert out["price"] == 347.5
    # Implausible stored value MUST get filtered to None.
    assert out["change_pct"] is None


@pytest.mark.asyncio
async def test_db_snapshot_tier_keeps_plausible_change_pct():
    """Don't be over-zealous — a stored ±5% snapshot value (a real
    move from before upstream went out) should still come through."""
    good_snap = {
        "price": 100.0,
        "volume": 12_000,
        "prev_close": 95.24,
        "data_source": "twse",
        "change_pct": 5.0,
    }
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_realtime_quote",
                      new=AsyncMock(return_value=None)), \
         patch.object(svc.finmind, "get_daily_ohlcv",
                      new=AsyncMock(return_value=[])), \
         patch(
             "services.ingest.repository.read_latest_quote_autosession",
             new=AsyncMock(return_value=good_snap),
         ):
        out = await svc.get_quote("2330")

    assert out["change_pct"] == 5.0


# ── archive-backed prev_close backfill ───────────────────────────


@pytest.mark.asyncio
async def test_fetch_quote_waterfall_backfills_prev_close_from_archive():
    """TWSE realtime returns close + change but no prev_close.
    Archive lookup yields yesterday's close. Result: change_pct
    computed from archive baseline, NOT from upstream's
    potentially-wrong `Change` field."""
    twse_payload = {
        "symbol": "2455", "name_zh": "全新",
        # Real upstream-style row: close present, change is junk
        # (TWSE puts the prior-day close in the `Change` slot for
        # some KY-listed stocks).
        "close": 347.5, "change": 315.84,
        "open": 321.0, "high": 347.5, "low": 321.0,
        "volume": 24_628_405,
    }
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with patch.object(svc, "cache_get_json",
                      new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json",
                      new_callable=AsyncMock), \
         patch.object(svc.twse, "get_realtime_quote",
                      new=AsyncMock(return_value=twse_payload)), \
         patch(
             "services.ingest.repository.read_ohlcv_range_autosession",
             new=AsyncMock(return_value=[
                 # Yesterday's close — archive truth, not the +315
                 # garbage TWSE handed us in `change`.
                 {"time": yesterday, "open": 320, "high": 322,
                  "low": 318, "close": 321.5, "volume": 1_000_000},
             ]),
         ):
        raw, source = await svc.fetch_quote_waterfall("2455")

    assert source == "twse"
    assert raw is not None
    assert raw["prev_close"] == 321.5


@pytest.mark.asyncio
async def test_get_quote_uses_archive_baseline_for_change_pct():
    """End-to-end: TWSE returns the buggy KY-style row, archive has
    yesterday's close, the user-visible change_pct is correct
    (~+8%), not the +992% the sanity bound would otherwise hide."""
    twse_payload = {
        "symbol": "2455", "name_zh": "全新",
        "close": 347.5, "change": 315.84,
        "volume": 24_628_405,
        "open": 321.0, "high": 347.5, "low": 321.0,
    }
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    archive_bars = [
        {"time": yesterday, "open": 320, "high": 322,
         "low": 318, "close": 321.5, "volume": 1_000_000},
    ]
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_realtime_quote",
                      new=AsyncMock(return_value=twse_payload)), \
         patch(
             "services.ingest.repository.read_ohlcv_range_autosession",
             new=AsyncMock(return_value=archive_bars),
         ):
        out = await svc.get_quote("2455")

    assert out["price"] == 347.5
    expected_chg = 347.5 - 321.5
    assert out["change"] == expected_chg
    assert out["change_pct"] == round(expected_chg / 321.5 * 100, 4)
    # Sanity guard: the +992% headline must NEVER appear.
    assert abs(out["change_pct"]) < 30


@pytest.mark.asyncio
async def test_resolve_prev_close_returns_none_for_unknown_symbol():
    """No bars in archive → None → caller falls back to upstream's
    own `change` field. Doesn't crash, doesn't leak stale cache."""
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch(
             "services.ingest.repository.read_ohlcv_range_autosession",
             new=AsyncMock(return_value=[]),
         ):
        out = await svc._resolve_prev_close("9999", upstream_close=580.0)
    assert out is None


@pytest.mark.asyncio
async def test_resolve_prev_close_weekend_skips_back_one_session():
    """Saturday view: TWSE keeps serving Friday's close. Without
    the upstream-vs-archive comparison we'd return Friday's archive
    bar as `prev` — close == prev → 漲幅 0%. The fix: when upstream's
    close matches the archive's latest bar, the displayed close IS
    the most recent archived session, so prev should be the bar
    BEFORE that (Thursday)."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    today_iso = date.today().isoformat()
    archive_bars = [
        # Thursday's bar
        {"time": yesterday, "open": 575, "high": 580,
         "low": 573, "close": 575, "volume": 1_000_000},
        # Friday's bar — TWSE on Saturday returns this same close
        {"time": today_iso, "open": 576, "high": 582,
         "low": 575, "close": 580, "volume": 1_200_000},
    ]
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch(
             "services.ingest.repository.read_ohlcv_range_autosession",
             new=AsyncMock(return_value=archive_bars),
         ):
        # Upstream close = 580 (Friday's, which TWSE will keep
        # serving on Saturday). Same as archive's latest → resolver
        # walks back one session.
        out = await svc._resolve_prev_close("2330", upstream_close=580.0)
    # Thursday's close, NOT Friday's. Otherwise change_pct = 0.
    assert out == 575.0


@pytest.mark.asyncio
async def test_resolve_prev_close_intraday_uses_archive_latest():
    """Mid-market view: TWSE serves a fresh tick (e.g. 583) that
    differs from archive's latest (Friday's 580). The "previous"
    is then archive's latest."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    today_iso = date.today().isoformat()
    archive_bars = [
        {"time": yesterday, "open": 575, "high": 580,
         "low": 573, "close": 575, "volume": 1_000_000},
        {"time": today_iso, "open": 576, "high": 582,
         "low": 575, "close": 580, "volume": 1_200_000},
    ]
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch(
             "services.ingest.repository.read_ohlcv_range_autosession",
             new=AsyncMock(return_value=archive_bars),
         ):
        out = await svc._resolve_prev_close("2330", upstream_close=583.0)
    # Friday's close — last completed session before today's live tick.
    assert out == 580.0


@pytest.mark.asyncio
async def test_get_quote_does_not_show_zero_pct_on_weekend():
    """End-to-end: TWSE returns Friday's stale close on Saturday;
    archive has Thu+Fri. Result must be Friday's session move
    (Thu→Fri), not 0%."""
    twse_payload = {
        "symbol": "2330", "name_zh": "台積電",
        "close": 580.0, "open": 576.0, "high": 582.0, "low": 575.0,
        "volume": 1_200_000,
        # No `change` in this stale payload — TWSE serves an empty
        # delta on a closed market day too.
    }
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    today_iso = date.today().isoformat()
    archive_bars = [
        {"time": yesterday, "open": 575, "high": 580,
         "low": 573, "close": 575, "volume": 1_000_000},
        {"time": today_iso, "open": 576, "high": 582,
         "low": 575, "close": 580, "volume": 1_200_000},
    ]
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_realtime_quote",
                      new=AsyncMock(return_value=twse_payload)), \
         patch(
             "services.ingest.repository.read_ohlcv_range_autosession",
             new=AsyncMock(return_value=archive_bars),
         ):
        out = await svc.get_quote("2330")

    assert out["price"] == 580.0
    # Real Friday-session move: 580 - 575 = 5, +0.87%
    assert out["change"] == 5.0
    assert out["change_pct"] == round(5 / 575 * 100, 4)
    assert out["change_pct"] != 0.0


@pytest.mark.asyncio
async def test_resolve_prev_close_rejects_stale_archive_then_falls_through_to_finmind():
    """Stale-archive guard: an ETF that recently went ex-distribution
    can leave the cron's last-ingested bar months behind today's price
    (e.g. 00713 archive 73.71 vs today's 52.85). Returning that bar as
    `prev` would produce a -28% headline that's pure cron-lag artefact.

    When the latest archived bar is >7 days old, the resolver skips
    the archive and asks FinMind for a fresh daily bar — so 昨收 still
    populates with reality (53.10) rather than going blank.
    """
    stale_date = (date.today() - timedelta(days=30)).isoformat()
    archive_bars = [
        {"time": stale_date, "open": 72, "high": 74,
         "low": 73, "close": 73.71, "volume": 1_000_000},
    ]
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    finmind_bars = [
        {"date": yesterday, "open": 52.5, "high": 53.2,
         "low": 52.0, "close": 53.10, "volume": 800_000},
    ]
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.finmind, "get_daily_ohlcv",
                      new=AsyncMock(return_value=finmind_bars)), \
         patch(
             "services.ingest.repository.read_ohlcv_range_autosession",
             new=AsyncMock(return_value=archive_bars),
         ):
        out = await svc._resolve_prev_close("00713", upstream_close=52.85)
    # Yesterday's FinMind close, not the months-stale 73.71.
    assert out == 53.10


@pytest.mark.asyncio
async def test_resolve_prev_close_finmind_fallback_when_archive_empty():
    """When ohlcv_daily has zero bars for a symbol (e.g. KY-listed
    stock the cron hasn't ingested yet), the resolver falls through to
    FinMind so 昨收 still has a real number — without this fallback
    the UI showed blank prev_close + the 998% upstream-change leak we
    eliminated by removing the derive-prev-from-change shortcut."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    finmind_bars = [
        {"date": yesterday, "open": 320, "high": 322,
         "low": 318, "close": 321.5, "volume": 1_000_000},
    ]
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.finmind, "get_daily_ohlcv",
                      new=AsyncMock(return_value=finmind_bars)), \
         patch(
             "services.ingest.repository.read_ohlcv_range_autosession",
             new=AsyncMock(return_value=[]),
         ):
        out = await svc._resolve_prev_close("2455", upstream_close=347.5)
    assert out == 321.5


@pytest.mark.asyncio
async def test_resolve_prev_close_accepts_fresh_archive():
    """Sanity: a same-day archive bar still flows through the resolver
    (this is the common intraday case)."""
    fresh_date = date.today().isoformat()
    archive_bars = [
        {"time": fresh_date, "open": 575, "high": 582,
         "low": 573, "close": 580, "volume": 1_200_000},
    ]
    with patch.object(svc, "cache_get_json", new=AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch(
             "services.ingest.repository.read_ohlcv_range_autosession",
             new=AsyncMock(return_value=archive_bars),
         ):
        out = await svc._resolve_prev_close("2330", upstream_close=583.0)
    assert out == 580.0
