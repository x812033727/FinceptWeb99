"""Pure unit tests for the TW market service's don't-cache-empty behavior.

When TWSE + FinMind + MOPS all fail, callers used to lock in a TTL_QUOTE
(60s) of zero-state data. Mirroring `us_market_service.get_quote` we now
skip cache_set when the result is empty so the next request retries.

Also exercises the Phase 2 MIS-Tier-0 integration: during TWSE regular
session the waterfall must hit `twse_mis` BEFORE `twse.get_realtime_quote`
so personas / watchlist callers see today's intraday tick instead of
yesterday's STOCK_DAY_ALL close.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services import tw_market_service as svc


@pytest.fixture(autouse=True)
def _default_finmind_not_preferred():
    """Pin FinMind-not-preferred (no token) by default so the EOD waterfall
    keeps its historical TWSE-first order — and so closed-market / MIS-fail
    tests never reach an unpatched live FinMind call. Tests that exercise
    the FinMind-first path override this with their own patch."""
    with patch.object(svc, "_finmind_preferred", AsyncMock(return_value=False)):
        yield


# ── get_quote ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_quote_zero_price_not_cached():
    """All upstream sources failed ⇒ price=0 ⇒ don't cache.

    Tier 0 (TWSE MIS intraday) is stubbed alongside the other tiers
    so the test is deterministic regardless of whether wall-clock
    NOW happens to be inside the regular session — without the stub
    the connector would otherwise attempt a real HTTP call to
    mis.twse.com.tw and either hang or randomly succeed depending
    on the test host.
    """
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.twse_mis, "get_realtime_quote", AsyncMock(return_value=None)), \
         patch.object(svc.twse, "get_realtime_quote", AsyncMock(side_effect=RuntimeError)), \
         patch.object(svc.finmind, "get_daily_ohlcv", AsyncMock(return_value=[])):
        result = await svc.get_quote("2330")

    mock_set.assert_not_awaited()
    assert result["price"] == 0


@pytest.mark.asyncio
async def test_get_quote_serves_mis_intraday_when_market_open():
    """During TWSE regular session, the MIS Tier 0 must take
    precedence. The `data_source` field is the operator-visible
    indicator that intraday data is flowing — falling through to
    `twse` (STOCK_DAY_ALL) silently is exactly the bug that
    motivated this phase.
    """
    mis_raw = {
        "symbol": "2330", "name_zh": "台積電",
        "close": 1095.0, "prev_close": 1090.0,
        "open": 1100.0, "high": 1105.0, "low": 1095.0,
        "volume": 12_345_600, "tlong": "1716797400000",
    }
    twse_called = AsyncMock(return_value={
        "close": 999, "prev_close": 999,  # poison value — must NOT win
    })
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc, "_is_tw_market_open", lambda: True), \
         patch.object(svc.twse_mis, "get_realtime_quote",
                      AsyncMock(return_value=mis_raw)), \
         patch.object(svc.twse, "get_realtime_quote", twse_called):
        result = await svc.get_quote("2330")

    twse_called.assert_not_awaited()
    assert result["price"] == 1095.0
    assert result["data_source"] == "twse_mis"
    # MIS is true intraday → personas should NOT treat it as a
    # historical close anchor.
    assert result["is_intraday"] is True


@pytest.mark.asyncio
async def test_get_quote_skips_mis_outside_session():
    """Pre-open / post-close / weekend — MIS would just return
    yesterday's last value and we'd burn a wasted HTTP RTT. Tier 0
    must short-circuit on `_is_tw_market_open() == False` so the
    existing tiers stay in charge of the EOD anchor.
    """
    mis_called = AsyncMock(return_value=None)
    twse_raw = {
        "symbol": "2330", "name_zh": "台積電",
        "close": 1090.0, "prev_close": 1085.0, "change": 5.0,
        "open": 1088.0, "high": 1092.0, "low": 1085.0,
        "volume": 8_000_000,
    }
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc, "_is_tw_market_open", lambda: False), \
         patch.object(svc.twse_mis, "get_realtime_quote", mis_called), \
         patch.object(svc.twse, "get_realtime_quote", AsyncMock(return_value=twse_raw)):
        result = await svc.get_quote("2330")

    mis_called.assert_not_awaited()
    assert result["price"] == 1090.0
    assert result["data_source"] == "twse"
    assert result["is_intraday"] is False


@pytest.mark.asyncio
async def test_get_quote_mis_failure_falls_through_to_twse():
    """MIS connectivity issues (403 from missing JSESSIONID, network
    glitch, malformed JSON) must NOT blank the quote — Tier 1's
    STOCK_DAY_ALL still has yesterday's close, which beats nothing.
    """
    twse_raw = {
        "symbol": "2330", "name_zh": "台積電",
        "close": 1090.0, "prev_close": 1085.0, "change": 5.0,
        "open": 1088.0, "high": 1092.0, "low": 1085.0,
        "volume": 8_000_000,
    }
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc, "_is_tw_market_open", lambda: True), \
         patch.object(svc.twse_mis, "get_realtime_quote",
                      AsyncMock(side_effect=RuntimeError("boom"))), \
         patch.object(svc.twse, "get_realtime_quote",
                      AsyncMock(return_value=twse_raw)):
        result = await svc.get_quote("2330")

    assert result["price"] == 1090.0
    assert result["data_source"] == "twse"


@pytest.mark.asyncio
async def test_get_quote_real_price_is_cached():
    raw = {
        "symbol": "2330", "name_zh": "台積電",
        "close": 820.0, "prev_close": 815.0, "change": 5.0,
        "open": 818.0, "high": 825.0, "low": 815.0, "volume": 25_000_000,
    }
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.twse_mis, "get_realtime_quote", AsyncMock(return_value=None)), \
         patch.object(svc.twse, "get_realtime_quote", AsyncMock(return_value=raw)):
        result = await svc.get_quote("2330")

    mock_set.assert_awaited_once()
    assert result["price"] == 820.0
    # Freshness stamps (post-Phase-1) must accompany every cached
    # quote so the focus_briefs forwarder can carry the session
    # anchor onto the prompt.
    assert "as_of_session" in result
    assert "is_intraday" in result
    assert result["is_intraday"] is False  # STOCK_DAY_ALL = EOD


# ── get_quote closed-market ohlcv_daily self-heal ─────────────────
#
# When TW is not trading and `ohlcv_daily` already holds the latest
# complete session, get_quote serves the settled close deterministically
# (mirrors the screener fast-path) instead of leaning on the live
# STOCK_DAY_ALL feed's publication lag. This is what lets the daily
# auto-run discussion read the prior session pre-market WITHOUT a
# backtest anchor.

@pytest.mark.asyncio
async def test_get_quote_closed_market_serves_settled_ohlcv():
    """Market closed + ohlcv has the latest complete session ⇒ read the
    settled bar from `ohlcv_daily`, NOT the live waterfall."""
    from datetime import date

    sess = date(2026, 5, 28)
    bars = [
        {"time": "2026-05-27", "open": 1000, "high": 1010, "low": 995,
         "close": 1005.0, "volume": 9_000},
        {"time": "2026-05-28", "open": 1006, "high": 1020, "low": 1004,
         "close": 1018.0, "volume": 11_000},
    ]
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc, "_is_tw_market_open", lambda: False), \
         patch.object(svc, "get_latest_ohlcv_session",
                      AsyncMock(return_value=sess)), \
         patch.object(svc, "_latest_complete_session", lambda: sess), \
         patch("services.ingest.repository.read_ohlcv_range_autosession",
               AsyncMock(return_value=bars)), \
         patch.object(svc, "fetch_quote_waterfall",
                      AsyncMock(side_effect=AssertionError(
                          "live waterfall must not run"))) as waterfall:
        result = await svc.get_quote("2330")

    waterfall.assert_not_awaited()
    assert result["price"] == 1018.0
    assert result["change"] == 13.0  # derived from prev close 1005 (bars[-2])
    assert result["data_source"] == "ohlcv_daily_today"
    assert result["as_of_session"] == "2026-05-28"
    assert result["is_intraday"] is False
    mock_set.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_quote_closed_market_falls_back_when_ohlcv_lagging():
    """Market closed but `ohlcv_daily` hasn't caught up to the latest
    complete session (ingest lag) ⇒ skip the fast-path, fall through to
    the live waterfall. This is the genuine 'missing today's data →
    fall back to live' path."""
    from datetime import date

    twse_raw = {
        "symbol": "2330", "name_zh": "台積電",
        "close": 1018.0, "prev_close": 1005.0, "change": 13.0,
        "open": 1006.0, "high": 1020.0, "low": 1004.0, "volume": 11_000,
    }
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc, "_is_tw_market_open", lambda: False), \
         patch.object(svc, "get_latest_ohlcv_session",
                      AsyncMock(return_value=date(2026, 5, 26))), \
         patch.object(svc, "_latest_complete_session",
                      lambda: date(2026, 5, 28)), \
         patch("services.ingest.repository.read_ohlcv_range_autosession",
               AsyncMock(return_value=[])) as db_read, \
         patch.object(svc.twse_mis, "get_realtime_quote",
                      AsyncMock(return_value=None)), \
         patch.object(svc.twse, "get_realtime_quote",
                      AsyncMock(return_value=twse_raw)):
        result = await svc.get_quote("2330")

    db_read.assert_not_awaited()  # gate fails before the bar read
    assert result["price"] == 1018.0
    assert result["data_source"] == "twse"


@pytest.mark.asyncio
async def test_get_quote_market_open_skips_settled_fast_path():
    """During the regular session the fast-path must NOT fire — intraday
    callers still go through the live waterfall (MIS Tier 0)."""
    from datetime import date

    mis_raw = {
        "symbol": "2330", "name_zh": "台積電",
        "close": 1095.0, "prev_close": 1090.0,
        "open": 1100.0, "high": 1105.0, "low": 1095.0,
        "volume": 12_345_600, "tlong": "1716797400000",
    }
    ohlcv_probe = AsyncMock(return_value=date(2026, 5, 28))
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc, "_is_tw_market_open", lambda: True), \
         patch.object(svc, "get_latest_ohlcv_session", ohlcv_probe), \
         patch.object(svc.twse_mis, "get_realtime_quote",
                      AsyncMock(return_value=mis_raw)):
        result = await svc.get_quote("2330")

    ohlcv_probe.assert_not_awaited()  # fast-path gated out when open
    assert result["price"] == 1095.0
    assert result["data_source"] == "twse_mis"


# ── fetch_quote_waterfall: FinMind-first ordering (token-gated) ───

@pytest.mark.asyncio
async def test_quote_waterfall_finmind_first_when_token_present():
    """Token configured + market closed ⇒ FinMind EOD is tried BEFORE
    TWSE STOCK_DAY_ALL."""
    fm_bars = [
        {"time": "2026-05-27", "open": 1000, "high": 1010, "low": 995,
         "close": 1005.0, "volume": 9_000},
        {"time": "2026-05-28", "open": 1006, "high": 1020, "low": 1004,
         "close": 1018.0, "volume": 11_000},
    ]
    twse_poison = AsyncMock(return_value={"close": 999, "prev_close": 999})
    with patch.object(svc, "_is_tw_market_open", lambda: False), \
         patch.object(svc, "_finmind_preferred", AsyncMock(return_value=True)), \
         patch.object(svc.finmind, "get_daily_ohlcv",
                      AsyncMock(return_value=fm_bars)), \
         patch.object(svc.twse, "get_realtime_quote", twse_poison):
        raw, source = await svc.fetch_quote_waterfall("2330")

    twse_poison.assert_not_awaited()  # FinMind won; TWSE not consulted
    assert source == "finmind"
    assert raw["close"] == 1018.0
    assert raw["prev_close"] == 1005.0


@pytest.mark.asyncio
async def test_quote_waterfall_twse_first_when_no_token():
    """No FinMind token ⇒ keep TWSE-first; FinMind not consulted once TWSE
    returns data."""
    twse_raw = {
        "symbol": "2330", "close": 1090.0, "prev_close": 1085.0,
        "open": 1088.0, "high": 1092.0, "low": 1085.0, "volume": 8_000_000,
    }
    fm = AsyncMock(return_value=[{"time": "2026-05-28", "close": 1.0,
                                  "open": 1, "high": 1, "low": 1, "volume": 1}])
    with patch.object(svc, "_is_tw_market_open", lambda: False), \
         patch.object(svc, "_finmind_preferred", AsyncMock(return_value=False)), \
         patch.object(svc.twse, "get_realtime_quote",
                      AsyncMock(return_value=twse_raw)), \
         patch.object(svc.finmind, "get_daily_ohlcv", fm):
        raw, source = await svc.fetch_quote_waterfall("2330")

    fm.assert_not_awaited()
    assert source == "twse"
    assert raw["close"] == 1090.0


@pytest.mark.asyncio
async def test_quote_waterfall_keeps_mis_intraday_priority():
    """During the regular session MIS Tier 0 wins regardless of FinMind
    preference — the EOD tiers (and the preference check) aren't reached."""
    mis_raw = {
        "symbol": "2330", "close": 1095.0, "prev_close": 1090.0,
        "open": 1100.0, "high": 1105.0, "low": 1095.0,
        "volume": 12_345_600, "tlong": "1716797400000",
    }
    prefer = AsyncMock(return_value=True)
    fm = AsyncMock(return_value=[])
    with patch.object(svc, "_is_tw_market_open", lambda: True), \
         patch.object(svc, "_finmind_preferred", prefer), \
         patch.object(svc.twse_mis, "get_realtime_quote",
                      AsyncMock(return_value=mis_raw)), \
         patch.object(svc.finmind, "get_daily_ohlcv", fm):
        raw, source = await svc.fetch_quote_waterfall("2330")

    prefer.assert_not_awaited()  # gate never reached — MIS filled raw
    fm.assert_not_awaited()
    assert source == "twse_mis"
    assert raw["close"] == 1095.0


# ── get_history ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_history_empty_not_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.twse, "get_daily_ohlcv", AsyncMock(side_effect=RuntimeError)), \
         patch.object(svc.finmind, "get_daily_ohlcv", AsyncMock(return_value=[])):
        result = await svc.get_history("2330", months=1)

    mock_set.assert_not_awaited()
    assert result == []


# ── get_institutional ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_institutional_empty_not_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.finmind, "get_institutional", AsyncMock(return_value=[])), \
         patch.object(svc.twse, "get_institutional", AsyncMock(return_value=[])):
        result = await svc.get_institutional("2330")

    mock_set.assert_not_awaited()
    assert result == []


# ── get_margin ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_margin_empty_not_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.finmind, "get_margin", AsyncMock(return_value=[])), \
         patch.object(svc.twse, "get_margin", AsyncMock(return_value=[])):
        result = await svc.get_margin("2330")

    mock_set.assert_not_awaited()
    assert result == []


# ── get_revenue ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_revenue_empty_not_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.finmind, "get_monthly_revenue", AsyncMock(return_value=[])), \
         patch.object(svc.mops, "get_monthly_revenue_recent", AsyncMock(return_value=[])):
        result = await svc.get_revenue("2330")

    mock_set.assert_not_awaited()
    assert result == []


# ── backtest mode (as_of) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_institutional_as_of_skips_cache_and_live_tiers():
    """Backtest mode bypasses Redis (per-as_of cache key would explode)
    and skips live FinMind / TWSE fallbacks (would return today's data,
    leaking future information into the historical replay). Returns
    whatever the DB archive holds — possibly []."""
    from datetime import date
    from unittest.mock import MagicMock

    finmind_mock = AsyncMock(return_value=[{"foreign_buy": 9999}])
    twse_mock = AsyncMock(return_value=[{"foreign_buy": 9999}])
    cache_get_mock = AsyncMock(return_value='[{"cached":"row"}]')
    db_rows = [{"date": "2026-04-15", "foreign_buy": 1000}]

    # Patch the lazy import inside the function: services.ingest.repository.read_institutional_range
    with patch.object(svc, "cache_get", cache_get_mock), \
         patch.object(svc, "cache_set", AsyncMock()) as cache_set_mock, \
         patch.object(svc.finmind, "get_institutional", finmind_mock), \
         patch.object(svc.twse, "get_institutional", twse_mock), \
         patch("services.ingest.repository.read_institutional_range",
               new=AsyncMock(return_value=db_rows)), \
         patch("db.session.AsyncSessionLocal", MagicMock()):
        result = await svc.get_institutional(
            "2330", days=30, as_of=date(2026, 4, 15),
        )

    # Cache GET / SET both skipped in backtest mode.
    cache_get_mock.assert_not_awaited()
    cache_set_mock.assert_not_awaited()
    # Live tiers must NOT have been called — would have returned 9999.
    finmind_mock.assert_not_awaited()
    twse_mock.assert_not_awaited()
    assert result == db_rows


@pytest.mark.asyncio
async def test_get_institutional_as_of_returns_empty_on_db_miss():
    """Backtest mode + DB has no rows in the historical window → []
    (NOT a live fallback). Caller already treats blank as 'no signal'."""
    from datetime import date
    from unittest.mock import MagicMock

    finmind_mock = AsyncMock(return_value=[{"foreign_buy": 9999}])
    twse_mock = AsyncMock(return_value=[{"foreign_buy": 9999}])

    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc.finmind, "get_institutional", finmind_mock), \
         patch.object(svc.twse, "get_institutional", twse_mock), \
         patch("services.ingest.repository.read_institutional_range",
               new=AsyncMock(return_value=[])), \
         patch("db.session.AsyncSessionLocal", MagicMock()):
        result = await svc.get_institutional(
            "2330", days=30, as_of=date(2020, 1, 1),
        )

    assert result == []
    finmind_mock.assert_not_awaited()  # critical: no future-leak
    twse_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_margin_as_of_skips_cache_and_live_tiers():
    from datetime import date
    from unittest.mock import MagicMock

    finmind_mock = AsyncMock(return_value=[{"margin_purchase_balance": 9999}])
    cache_get_mock = AsyncMock(return_value='[{"cached":"row"}]')
    db_rows = [{"date": "2026-04-15", "margin_purchase_balance": 1000}]

    with patch.object(svc, "cache_get", cache_get_mock), \
         patch.object(svc, "cache_set", AsyncMock()) as cache_set_mock, \
         patch.object(svc.finmind, "get_margin", finmind_mock), \
         patch("services.ingest.repository.read_margin_range",
               new=AsyncMock(return_value=db_rows)), \
         patch("db.session.AsyncSessionLocal", MagicMock()):
        result = await svc.get_margin(
            "2330", days=30, as_of=date(2026, 4, 15),
        )

    cache_get_mock.assert_not_awaited()
    cache_set_mock.assert_not_awaited()
    finmind_mock.assert_not_awaited()
    assert result == db_rows


@pytest.mark.asyncio
async def test_get_financials_as_of_filters_by_period_end_date():
    """Backtest mode filters FinMind rows by `date <= as_of` so a
    2024-Q4 anchor never sees Q1-2025 filings (which would be
    future-leak — the model would cite earnings that hadn't been
    reported yet at the historical anchor)."""
    from datetime import date

    rows = [
        {"date": "2024-09-30", "type": "EPS", "value": 12.3},   # Q3 2024 — keep
        {"date": "2024-12-31", "type": "EPS", "value": 13.5},   # Q4 2024 — keep
        {"date": "2025-03-31", "type": "EPS", "value": 14.0},   # Q1 2025 — drop
        {"date": "2025-06-30", "type": "EPS", "value": 15.2},   # Q2 2025 — drop
    ]
    cache_get_mock = AsyncMock(return_value='[{"cached":"row"}]')
    finmind_mock = AsyncMock(return_value=rows)

    with patch.object(svc, "cache_get", cache_get_mock), \
         patch.object(svc, "cache_set", AsyncMock()) as cache_set_mock, \
         patch.object(svc.finmind, "get_financials", finmind_mock):
        result = await svc.get_financials("2330", as_of=date(2025, 1, 31))

    cache_get_mock.assert_not_awaited()  # backtest bypasses Redis
    cache_set_mock.assert_not_awaited()  # ditto for write
    finmind_mock.assert_awaited_once()    # service still hits FinMind
    dates = [r["date"] for r in result]
    assert dates == ["2024-09-30", "2024-12-31"]


@pytest.mark.asyncio
async def test_get_financials_live_mode_uses_redis_cache():
    """Sanity: live mode (no as_of) keeps the existing cache-first
    path so we don't regress hot-path latency."""
    cache_get_mock = AsyncMock(return_value='[{"cached":"row"}]')

    with patch.object(svc, "cache_get", cache_get_mock), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc.finmind, "get_financials",
                      AsyncMock(return_value=[])) as finmind_mock:
        result = await svc.get_financials("2330")

    cache_get_mock.assert_awaited_once()
    finmind_mock.assert_not_awaited()  # cache hit short-circuits
    assert result == [{"cached": "row"}]


# ── get_fundamentals ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_fundamentals_no_ratios_not_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.twse, "get_valuation_ratios", AsyncMock(side_effect=RuntimeError)):
        result = await svc.get_fundamentals("2330")

    mock_set.assert_not_awaited()
    assert result["symbol"] == "2330"


@pytest.mark.asyncio
async def test_get_fundamentals_with_ratios_is_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.twse, "get_valuation_ratios", AsyncMock(return_value={"pe": 18.5, "pb": 5.2})):
        result = await svc.get_fundamentals("2330")

    mock_set.assert_awaited_once()
    assert result["pe"] == 18.5


# ── find_symbol_by_name_in_text (PR #215) ─────────────────────────


def test_find_symbol_by_name_returns_none_when_map_empty():
    """Fresh deploy before symbol-map cron has run → graceful None,
    not exception."""
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        assert svc.find_symbol_by_name_in_text("台積電法說") is None
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)


def test_find_symbol_by_name_matches_company_name_in_title():
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        svc._name_map.update({
            "2330": "台積電",
            "2454": "聯發科",
            "2317": "鴻海",
        })
        assert svc.find_symbol_by_name_in_text(
            "台積電法說會超預期 - 經濟日報",
        ) == "2330"
        assert svc.find_symbol_by_name_in_text(
            "聯發科 Q1 EPS 公布",
        ) == "2454"
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)


def test_find_symbol_by_name_prefers_longer_match_on_collision():
    """`中華電` contains `中華` as a prefix. Prefix collision must
    resolve to the longer name, otherwise CHT (2412) headlines
    would mis-tag as some 中華-prefixed code."""
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        svc._name_map.update({
            "2412": "中華電",
            "1234": "中華",   # shorter — must NOT win when both could match
        })
        assert svc.find_symbol_by_name_in_text(
            "中華電宣布漲價 - 工商時報",
        ) == "2412"
        # Pure 中華 (no 電) should still match the shorter name.
        assert svc.find_symbol_by_name_in_text(
            "中華大樓開幕",
        ) == "1234"
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)


def test_find_symbol_by_name_returns_none_when_no_match():
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        svc._name_map.update({"2330": "台積電"})
        assert svc.find_symbol_by_name_in_text("純策略討論不提具體公司") is None
        assert svc.find_symbol_by_name_in_text("") is None
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)


# ── find_symbols_by_names_in_text (PR #221 multi-match) ─────────


def test_find_symbols_by_names_returns_multiple_matches():
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        svc._name_map.update({
            "2330": "台積電", "2454": "聯發科", "2317": "鴻海",
        })
        out = svc.find_symbols_by_names_in_text(
            "討論台積電 / 鴻海 短線", limit=5,
        )
        assert "2330" in out
        assert "2317" in out
        # 聯發科 not in text → not in result
        assert "2454" not in out
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)


def test_find_symbols_by_names_caps_at_limit():
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        svc._name_map.update({
            "2330": "台積電", "2454": "聯發科",
            "2317": "鴻海", "2412": "中華電",
        })
        out = svc.find_symbols_by_names_in_text(
            "台積電 聯發科 鴻海 中華電 都漲", limit=2,
        )
        assert len(out) == 2
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)


def test_find_symbols_by_names_masks_consumed_spans_for_prefix_collision():
    """中華電 masks its own occurrences before 中華 scans, so the
    shorter name only matches positions 中華電 didn't consume.
    "中華地產 vs 中華電" → 中華電 (consumes its slot) + 中華
    (still hits in 中華地產)."""
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        svc._name_map.update({
            "2412": "中華電",
            "1234": "中華",   # shorter, also matches
        })
        out = svc.find_symbols_by_names_in_text(
            "中華地產 vs 中華電", limit=5,
        )
        # Both should land — 中華電 first (longer), then 中華 from
        # the part of the text that isn't already masked.
        assert out == ["2412", "1234"]


        # Sanity: pure 中華 (no 中華電) still matches the shorter.
        out = svc.find_symbols_by_names_in_text("中華大樓開幕", limit=5)
        assert out == ["1234"]
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)


def test_find_symbols_by_names_returns_empty_when_no_matches():
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        svc._name_map.update({"2330": "台積電"})
        assert svc.find_symbols_by_names_in_text("純策略討論", limit=5) == []
        assert svc.find_symbols_by_names_in_text("", limit=5) == []
        assert svc.find_symbols_by_names_in_text("台積電", limit=0) == []
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)


def test_find_symbols_by_names_empty_when_map_empty():
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        assert svc.find_symbols_by_names_in_text("台積電法說", limit=5) == []
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)
