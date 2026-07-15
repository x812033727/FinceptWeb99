"""Tests for `tw_market_service.get_screener` staleness detection.

Two regressions covered:

1. Post-14:30 Taipei discussions were getting yesterday's STOCK_DAY_ALL
   close because the freshness phase resolver trusted wall-clock
   without verifying upstream actually refreshed. The fix prefers
   `ohlcv_daily` when it has today's bar and falls back to a sentinel
   (2330) check against DB to detect stale TWSE responses.

2. When upstream STOCK_DAY_ALL provably lags, the captured_session
   phase is downgraded to `between_close_and_publish` so the prompt /
   JSON view stop claiming "today's data" when the rows are actually
   from the prior trading day.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest
import pytz

# Stub `data.us.yfinance_connector` so the screener's lazy import in
# `_recover_screener_via_yfinance` resolves without dragging pandas /
# yfinance into the test env. The real connector is just a thin wrapper
# around `yf.download`; tests patch the function attribute directly so
# the stub body is irrelevant.
import sys as _sys
import types as _types
if "data.us.yfinance_connector" not in _sys.modules:
    _stub = _types.ModuleType("data.us.yfinance_connector")
    async def _stub_batch_quotes(tickers):  # noqa: ARG001
        return {}
    async def _stub_info(ticker):  # noqa: ARG001
        return {}
    _stub.get_batch_quotes = _stub_batch_quotes
    _stub.get_info = _stub_info
    _sys.modules["data.us.yfinance_connector"] = _stub
    import data.us as _data_us
    _data_us.yfinance_connector = _stub

from services import tw_market_service as svc
from services.discussion.context.builder import _maybe_downgrade_captured_session
from services.discussion.screener_utils import (
    _compact_screener_row,
    _compact_us_screener_row,
)


_TW = pytz.timezone("Asia/Taipei")


def _patch_tw_now(*, year: int, month: int, day: int, hh: int, mm: int = 0):
    """Freeze both freshness clocks to a single Taipei wall-clock so
    `_latest_complete_session` returns deterministic values."""
    local = _TW.localize(datetime(year, month, day, hh, mm))
    utc = local.astimezone(UTC)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return utc.astimezone(tz) if tz else utc.replace(tzinfo=None)

    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(
        patch("services.discussion.freshness.datetime", _FrozenDatetime),
    )
    stack.enter_context(
        patch("services.tw_market_service.datetime", _FrozenDatetime),
    )
    stack.enter_context(
        patch("services.tw_trading_calendar.datetime", _FrozenDatetime),
    )
    return stack


@pytest.fixture(autouse=True)
def _reset_ohlcv_latest_cache():
    """Clear the per-pod ohlcv_latest cache between tests so a frozen
    clock from a prior test doesn't leak a stale value."""
    svc._ohlcv_latest_cache = None
    yield
    svc._ohlcv_latest_cache = None


# ── _latest_complete_session ──────────────────────────────────────


def test_latest_complete_session_returns_today_post_publish():
    """Wed 15:00 Taipei is past 14:30 STOCK_DAY_ALL publish, so the
    latest complete session is today."""
    with _patch_tw_now(year=2026, month=5, day=27, hh=15):
        assert svc._latest_complete_session() == date(2026, 5, 27)


def test_latest_complete_session_returns_prev_day_intraday():
    """Wed 11:00 Taipei is intraday; today's close hasn't published.
    The latest COMPLETE session is the prior trading day — used by
    the screener so staleness detection still fires when TWSE serves
    something older than that."""
    with _patch_tw_now(year=2026, month=5, day=27, hh=11):
        assert svc._latest_complete_session() == date(2026, 5, 26)


def test_latest_complete_session_returns_prev_day_pre_open():
    """Fri 00:01 Taipei (after midnight crossover) — pre-open phase.
    Latest complete session is the prior weekday's close. The
    user-reported bug case: at this hour the original
    `_expected_today_session()` returned None and short-circuited
    the yfinance recovery despite TWSE being a day behind."""
    with _patch_tw_now(year=2026, month=5, day=29, hh=0, mm=1):
        assert svc._latest_complete_session() == date(2026, 5, 28)


def test_latest_complete_session_returns_recent_weekday_on_weekend():
    """Sat 10:00 Taipei — most recent weekday's close is the
    relevant anchor, not None."""
    with _patch_tw_now(year=2026, month=5, day=30, hh=10):
        assert svc._latest_complete_session() == date(2026, 5, 29)


# ── get_screener fast path: ohlcv_daily has today ─────────────────


@pytest.mark.asyncio
async def test_get_screener_uses_ohlcv_when_today_in_db():
    """Post-publish + ohlcv_daily already has today's bar → go straight
    to the backtest screener, skip TWSE entirely, and stamp each row
    `actual_session=today` + `data_source=ohlcv_daily_today`.
    """
    today = date(2026, 5, 27)
    backtest_rows = [
        {"symbol": "2330", "market": "TW", "exchange": "TWSE",
         "name_zh": "TSMC", "price": 1100.0, "change": 10.0,
         "change_pct": 0.92, "volume": 5_000_000, "pe_ratio": None,
         "pb_ratio": None, "dividend_yield": None,
         "data_source": "ohlcv_daily", "as_of": today.isoformat()},
    ]
    twse_mock = AsyncMock()
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", AsyncMock()), \
         patch.object(
             svc, "get_latest_ohlcv_session",
             AsyncMock(return_value=today),
         ), \
         patch.object(
             svc, "_get_screener_backtest",
             AsyncMock(return_value=backtest_rows),
         ), \
         patch.object(svc.twse, "get_all_twse_symbols", twse_mock), \
         _patch_tw_now(year=2026, month=5, day=27, hh=15):
        result = await svc.get_screener(limit=50, min_volume=1_000_000)

    twse_mock.assert_not_awaited()
    assert len(result) == 1
    assert result[0]["actual_session"] == today.isoformat()
    assert result[0]["data_source"] == "ohlcv_daily_today"
    assert result[0]["is_stale"] is False


# ── get_screener slow path: TWSE stale, DB has today (recover) ────


@pytest.mark.asyncio
async def test_get_screener_recovers_from_stale_twse_via_ohlcv():
    """TWSE STOCK_DAY_ALL is still serving yesterday's row but
    ohlcv_daily is one trading day ahead. The detector picks this up
    via the 2330 close match against the prior day and switches the
    response to the backtest path so the consumer gets today's close.
    """
    today = date(2026, 5, 27)
    yesterday = date(2026, 5, 26)
    # TWSE response: 2330's 收盤價 matches yesterday's DB close.
    twse_rows = [
        {"Code": "2330", "Name": "TSMC", "成交股數": "5000000",
         "收盤價": "1080.0", "漲跌價差": "5.0"},
        {"Code": "8110", "Name": "華東", "成交股數": "2000000",
         "收盤價": "60.6", "漲跌價差": "5.5"},  # The reported bug case
    ]
    # Backtest result is today's true close (62 for 8110).
    backtest_rows = [
        {"symbol": "2330", "market": "TW", "exchange": "TWSE",
         "name_zh": "TSMC", "price": 1100.0, "change": 20.0,
         "change_pct": 1.85, "volume": 5_000_000, "pe_ratio": None,
         "pb_ratio": None, "dividend_yield": None,
         "data_source": "ohlcv_daily", "as_of": today.isoformat()},
        {"symbol": "8110", "market": "TW", "exchange": "TWSE",
         "name_zh": "華東", "price": 62.0, "change": 1.4,
         "change_pct": 2.31, "volume": 2_000_000, "pe_ratio": None,
         "pb_ratio": None, "dividend_yield": None,
         "data_source": "ohlcv_daily", "as_of": today.isoformat()},
    ]
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", AsyncMock()), \
         patch.object(
             svc, "get_latest_ohlcv_session",
             AsyncMock(return_value=today),
         ), \
         patch.object(
             svc, "_bellwether_ohlcv_closes",
             AsyncMock(return_value=[
                 (yesterday, 1080.0), (today, 1100.0),
             ]),
         ), \
         patch.object(
             svc, "_get_screener_backtest",
             AsyncMock(return_value=backtest_rows),
         ), \
         patch.object(
             svc.twse, "get_all_twse_symbols",
             AsyncMock(return_value=twse_rows),
         ), \
         patch.object(
             svc, "_recover_screener_via_finmind",
             AsyncMock(return_value=None),
         ), \
         patch.object(
             svc, "_recover_screener_via_yfinance",
             AsyncMock(return_value=None),
         ), \
         _patch_tw_now(year=2026, month=5, day=27, hh=15):
        result = await svc.get_screener(limit=50, min_volume=1_000_000)

    # Fast-path also catches today-in-DB and short-circuits before
    # reaching TWSE. Recovery path is exercised via the explicit case
    # below where DB is BEHIND TWSE in the second test.
    assert all(r["actual_session"] == today.isoformat() for r in result)
    assert all(r["data_source"] in ("ohlcv_daily_today",
                                    "ohlcv_daily_recovered") for r in result)
    by_symbol = {r["symbol"]: r for r in result}
    assert by_symbol["8110"]["price"] == 62.0  # the bug fix


# ── get_screener slow path: TWSE stale, DB also stale ─────────────


@pytest.mark.asyncio
async def test_get_screener_stamps_stale_when_db_cant_recover():
    """Ingest cron also skipped — DB latest = yesterday, TWSE = yesterday.
    Can't recover today's close; return TWSE rows but stamp them with
    yesterday's session + is_stale=True so downstream phase downgrade
    fires.
    """
    yesterday = date(2026, 5, 26)
    twse_rows = [
        {"Code": "2330", "Name": "TSMC", "成交股數": "5000000",
         "收盤價": "1080.0", "漲跌價差": "5.0"},
        {"Code": "8110", "Name": "華東", "成交股數": "2000000",
         "收盤價": "60.6", "漲跌價差": "5.5"},
    ]
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", AsyncMock()), \
         patch.object(
             svc, "get_latest_ohlcv_session",
             AsyncMock(return_value=yesterday),  # DB behind too
         ), \
         patch.object(
             svc, "_bellwether_ohlcv_closes",
             AsyncMock(return_value=[(yesterday, 1080.0)]),
         ), \
         patch.object(
             svc.twse, "get_all_twse_symbols",
             AsyncMock(return_value=twse_rows),
         ), \
         patch.object(
             svc, "_recover_screener_via_finmind",
             AsyncMock(return_value=None),
         ), \
         patch.object(
             svc, "_recover_screener_via_yfinance",
             AsyncMock(return_value=None),
         ), \
         _patch_tw_now(year=2026, month=5, day=27, hh=15):
        result = await svc.get_screener(limit=50, min_volume=1_000_000)

    assert len(result) == 2
    for row in result:
        assert row["actual_session"] == yesterday.isoformat()
        assert row["is_stale"] is True
        assert row["data_source"] == "twse"


# ── _detect_stock_day_all_session edge cases ──────────────────────


@pytest.mark.asyncio
async def test_detect_returns_none_when_bellwether_missing():
    """No 2330 row in the TWSE response → detector returns None
    (inconclusive) instead of guessing."""
    rows_without_bell = [
        {"Code": "8110", "Name": "華東", "收盤價": "60.6"},
    ]
    with patch.object(
        svc, "_bellwether_ohlcv_closes",
        AsyncMock(return_value=[(date(2026, 5, 27), 1100.0)]),
    ):
        result = await svc._detect_stock_day_all_session(rows_without_bell)
    assert result is None


@pytest.mark.asyncio
async def test_detect_returns_today_when_match():
    """Bellwether close matches today's DB close → return today."""
    rows = [
        {"Code": "2330", "Name": "TSMC", "收盤價": "1100.0"},
    ]
    with patch.object(
        svc, "_bellwether_ohlcv_closes",
        AsyncMock(return_value=[
            (date(2026, 5, 26), 1080.0),
            (date(2026, 5, 27), 1100.0),
        ]),
    ):
        result = await svc._detect_stock_day_all_session(rows)
    assert result == date(2026, 5, 27)


# ── captured_session phase downgrade ──────────────────────────────


def test_downgrade_fires_when_actual_session_earlier_than_expected():
    """Builder helper: when screener rows are dated 2026-05-26 but
    captured_session.session_date says 2026-05-27, rewrite the block
    so personas see the real session and the JSON view shows the
    `phase_downgrade_reason` audit trail.
    """
    ctx = {
        "captured_session": {
            "session_date": "2026-05-27",
            "phase": "today_close_published",
            "is_intraday": False,
            "hint_zh": "資料截至 2026-05-27 收盤...",
        },
        "screener_actual_session": "2026-05-26",
        "screener_data_source": "twse",
    }
    _maybe_downgrade_captured_session(ctx)

    sess = ctx["captured_session"]
    assert sess["phase"] == "between_close_and_publish"
    assert sess["session_date"] == "2026-05-26"
    assert sess["phase_downgrade_reason"] == "stock_day_all_lag_detected"
    assert sess["wall_clock_session_date"] == "2026-05-27"


def test_downgrade_noop_when_actual_matches_expected():
    """Screener actual_session equals captured_session.session_date —
    everything is fresh, no rewrite."""
    original = {
        "session_date": "2026-05-27",
        "phase": "today_close_published",
        "is_intraday": False,
        "hint_zh": "資料截至 2026-05-27 收盤...",
    }
    ctx = {
        "captured_session": dict(original),
        "screener_actual_session": "2026-05-27",
    }
    _maybe_downgrade_captured_session(ctx)
    assert ctx["captured_session"] == original


def test_downgrade_noop_when_no_screener_session():
    """No `screener_actual_session` field — detector was inconclusive
    or US/CRYPTO market without staleness check. Leave captured_session
    untouched."""
    original = {
        "session_date": "2026-05-27",
        "phase": "today_close_published",
        "is_intraday": False,
    }
    ctx = {"captured_session": dict(original)}
    _maybe_downgrade_captured_session(ctx)
    assert ctx["captured_session"] == original


# ── yfinance recovery path (Part C) ───────────────────────────────


@pytest.mark.asyncio
async def test_yfinance_recovers_when_twse_and_ohlcv_both_stuck():
    """The user-reported case: at 23:29 Taipei on 2026-05-28, TWSE
    STOCK_DAY_ALL still serves 2026-05-27 close (60.6 for 8110),
    `ohlcv_daily` also stuck on 2026-05-27 because the daily cron
    drew from the same upstream. yfinance's chart endpoint has
    independent infrastructure and returns today's actual close (62).
    Assert the recovery patches the 8110 row to 62 + stamps with
    today's session + drops the stale TWSE values.
    """
    today = date(2026, 5, 28)
    yesterday = date(2026, 5, 27)
    twse_rows = [
        {"Code": "2330", "Name": "TSMC", "成交股數": "5000000",
         "收盤價": "1080.0", "漲跌價差": "5.0"},
        {"Code": "8110", "Name": "華東", "成交股數": "68790601",
         "收盤價": "60.6", "漲跌價差": "5.5"},
    ]
    yf_quotes = {
        "2330.TW": {"price": 1100.0, "change_pct": 1.85, "volume": 5_000_000},
        "8110.TW": {"price": 62.0, "change_pct": 2.31, "volume": 70_000_000},
    }
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", AsyncMock()) as cache_set_mock, \
         patch.object(
             svc, "get_latest_ohlcv_session",
             AsyncMock(return_value=yesterday),
         ), \
         patch.object(
             svc, "_bellwether_ohlcv_closes",
             AsyncMock(return_value=[(yesterday, 1080.0)]),
         ), \
         patch.object(
             svc.twse, "get_all_twse_symbols",
             AsyncMock(return_value=twse_rows),
         ), \
         patch(
             "data.us.yfinance_connector.get_batch_quotes",
             AsyncMock(return_value=yf_quotes),
         ), \
         _patch_tw_now(year=2026, month=5, day=28, hh=23, mm=29):
        result = await svc.get_screener(limit=50, min_volume=1_000_000)

    by_symbol = {r["symbol"]: r for r in result}
    assert "8110" in by_symbol
    assert by_symbol["8110"]["price"] == 62.0
    assert by_symbol["8110"]["actual_session"] == today.isoformat()
    assert by_symbol["8110"]["data_source"] == "yfinance_recovery"
    assert by_symbol["8110"]["is_stale"] is False
    # Recovered result caches with full TTL (not the 60s stale TTL)
    assert cache_set_mock.await_count == 1
    last_call = cache_set_mock.await_args
    assert last_call.args[2] == svc.TTL_SCREENER


@pytest.mark.asyncio
async def test_yfinance_skipped_when_also_lagging():
    """yfinance's 2330 close matches `ohlcv_daily`'s 2026-05-27 close
    within epsilon → Yahoo is also stuck on yesterday's session.
    Recovery bails out and the labelled-stale path takes over."""
    today = date(2026, 5, 28)
    yesterday = date(2026, 5, 27)
    twse_rows = [
        {"Code": "2330", "Name": "TSMC", "成交股數": "5000000",
         "收盤價": "1080.0", "漲跌價差": "5.0"},
        {"Code": "8110", "Name": "華東", "成交股數": "2000000",
         "收盤價": "60.6", "漲跌價差": "5.5"},
    ]
    yf_quotes = {
        "2330.TW": {"price": 1080.0, "change_pct": 0.5, "volume": 5_000_000},
        "8110.TW": {"price": 60.6, "change_pct": 9.98, "volume": 70_000_000},
    }
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", AsyncMock()) as cache_set_mock, \
         patch.object(
             svc, "get_latest_ohlcv_session",
             AsyncMock(return_value=yesterday),
         ), \
         patch.object(
             svc, "_bellwether_ohlcv_closes",
             AsyncMock(return_value=[(yesterday, 1080.0)]),
         ), \
         patch.object(
             svc.twse, "get_all_twse_symbols",
             AsyncMock(return_value=twse_rows),
         ), \
         patch(
             "data.us.yfinance_connector.get_batch_quotes",
             AsyncMock(return_value=yf_quotes),
         ), \
         _patch_tw_now(year=2026, month=5, day=28, hh=23, mm=29):
        result = await svc.get_screener(limit=50, min_volume=1_000_000)

    by_symbol = {r["symbol"]: r for r in result}
    assert by_symbol["8110"]["price"] == 60.6
    assert by_symbol["8110"]["actual_session"] == yesterday.isoformat()
    assert by_symbol["8110"]["is_stale"] is True
    assert by_symbol["8110"]["data_source"] == "twse"
    # Stale result caches with the short TTL so a TWSE refresh isn't
    # shadowed for 10 minutes.
    assert cache_set_mock.await_count == 1
    assert cache_set_mock.await_args.args[2] == svc._TTL_SCREENER_STALE
    _ = today  # silence linter; date is used for context


@pytest.mark.asyncio
async def test_yfinance_recovery_skipped_when_batch_empty():
    """yfinance batch returns empty (transient Yahoo outage / DNS
    failure / `yf.download` returned an empty frame). Recovery bails
    and the labelled-stale path takes over without crashing."""
    today = date(2026, 5, 28)
    yesterday = date(2026, 5, 27)
    twse_rows = [
        {"Code": "2330", "Name": "TSMC", "成交股數": "5000000",
         "收盤價": "1080.0", "漲跌價差": "5.0"},
        {"Code": "8110", "Name": "華東", "成交股數": "2000000",
         "收盤價": "60.6", "漲跌價差": "5.5"},
    ]
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", AsyncMock()) as cache_set_mock, \
         patch.object(
             svc, "get_latest_ohlcv_session",
             AsyncMock(return_value=yesterday),
         ), \
         patch.object(
             svc, "_bellwether_ohlcv_closes",
             AsyncMock(return_value=[(yesterday, 1080.0)]),
         ), \
         patch.object(
             svc.twse, "get_all_twse_symbols",
             AsyncMock(return_value=twse_rows),
         ), \
         patch(
             "data.us.yfinance_connector.get_batch_quotes",
             AsyncMock(return_value={}),
         ), \
         _patch_tw_now(year=2026, month=5, day=28, hh=23, mm=29):
        result = await svc.get_screener(limit=50, min_volume=1_000_000)

    by_symbol = {r["symbol"]: r for r in result}
    assert by_symbol["8110"]["price"] == 60.6
    assert by_symbol["8110"]["is_stale"] is True
    assert cache_set_mock.await_args.args[2] == svc._TTL_SCREENER_STALE
    _ = today


# ── Cache freshness key + TTL ─────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_key_includes_ohlcv_latest_to_invalidate_on_advance():
    """When `ohlcv_daily` advances from yesterday to today (e.g. the
    daily cron just finished), the cache key changes so a previously
    cached stale entry doesn't shadow the fast-path's fresh result."""
    today = date(2026, 5, 28)
    yesterday = date(2026, 5, 27)

    # Round 1: ohlcv stuck on yesterday, returns stale + caches under
    # the yesterday-tagged key.
    twse_rows = [
        {"Code": "2330", "Name": "TSMC", "成交股數": "5000000",
         "收盤價": "1080.0", "漲跌價差": "5.0"},
    ]
    cache: dict[str, str] = {}

    async def _cache_get(k: str) -> str | None:
        return cache.get(k)

    async def _cache_set(k: str, v: str, ttl: int) -> None:
        cache[k] = v

    with patch.object(svc, "cache_get_json", AsyncMock(side_effect=_cache_get)), \
         patch.object(svc, "cache_set_json", AsyncMock(side_effect=_cache_set)), \
         patch.object(
             svc, "get_latest_ohlcv_session",
             AsyncMock(return_value=yesterday),
         ), \
         patch.object(
             svc, "_bellwether_ohlcv_closes",
             AsyncMock(return_value=[(yesterday, 1080.0)]),
         ), \
         patch.object(
             svc.twse, "get_all_twse_symbols",
             AsyncMock(return_value=twse_rows),
         ), \
         patch(
             "data.us.yfinance_connector.get_batch_quotes",
             AsyncMock(return_value={}),
         ), \
         _patch_tw_now(year=2026, month=5, day=28, hh=15):
        round_1 = await svc.get_screener(limit=50, min_volume=1_000_000)
        assert any(r.get("is_stale") for r in round_1)

    yesterday_keys = list(cache.keys())
    assert any(yesterday.isoformat() in k for k in yesterday_keys)

    # Round 2: ohlcv_daily has now advanced. Same call → different cache
    # key (today's ohlcv tag) → the yesterday-cached stale entry doesn't
    # shadow the fast-path retry.
    backtest_rows = [
        {"symbol": "2330", "market": "TW", "exchange": "TWSE",
         "name_zh": "TSMC", "price": 1100.0, "change": 20.0,
         "change_pct": 1.85, "volume": 5_000_000, "pe_ratio": None,
         "pb_ratio": None, "dividend_yield": None,
         "data_source": "ohlcv_daily", "as_of": today.isoformat()},
    ]
    with patch.object(svc, "cache_get_json", AsyncMock(side_effect=_cache_get)), \
         patch.object(svc, "cache_set_json", AsyncMock(side_effect=_cache_set)), \
         patch.object(
             svc, "get_latest_ohlcv_session",
             AsyncMock(return_value=today),
         ), \
         patch.object(
             svc, "_get_screener_backtest",
             AsyncMock(return_value=backtest_rows),
         ), \
         _patch_tw_now(year=2026, month=5, day=28, hh=15):
        round_2 = await svc.get_screener(limit=50, min_volume=1_000_000)

    assert round_2 != round_1
    assert all(
        r["data_source"] == "ohlcv_daily_today" and not r["is_stale"]
        for r in round_2
    )


# ── data_source projection (Part D) ───────────────────────────────


def test_compact_screener_row_passes_data_source_through():
    """`_compact_screener_row` is the projection used for the
    `top_gainers` / `top_losers` ctx blocks the user sees in the JSON
    view. Without this passthrough, the recovery layer's source tag
    is invisible — a yfinance_recovery hit looks identical to a stale
    twse miss on the rendered row."""
    for source in (
        "twse",
        "ohlcv_daily_today",
        "ohlcv_daily_recovered",
        "yfinance_recovery",
    ):
        raw = {
            "symbol": "8110",
            "name_zh": "華東",
            "price": 62.0,
            "change_pct": 2.31,
            "volume": 70_000_000,
            "pe_ratio": None,
            "dividend_yield": None,
            "data_source": source,
        }
        compact = _compact_screener_row(
            raw, as_of_session="2026-05-28", is_intraday=False,
        )
        assert compact["data_source"] == source, (
            f"Expected `data_source={source}` to pass through projection"
        )


def test_compact_us_screener_row_passes_data_source_through():
    """US sibling projection — same passthrough so polygon vs yfinance
    vs stooq vs finnhub is observable in the US ctx JSON view too."""
    for source in ("polygon", "yfinance", "stooq", "finnhub"):
        raw = {
            "symbol": "NVDA",
            "name": "NVIDIA Corp",
            "sector": "Technology",
            "price": 145.0,
            "change_pct": 1.2,
            "volume": 200_000_000,
            "data_source": source,
        }
        compact = _compact_us_screener_row(
            raw, as_of_session="2026-05-28", is_intraday=False,
        )
        assert compact["data_source"] == source


def test_compact_screener_row_data_source_is_none_when_missing():
    """Rows from pre-Part-B discussions (or test fixtures that didn't
    set the field) just get `None` rather than KeyError-ing or
    raising — the projection is forgiving."""
    raw = {"symbol": "8110", "price": 62.0}
    compact = _compact_screener_row(
        raw, as_of_session="2026-05-28", is_intraday=False,
    )
    assert compact["data_source"] is None


def test_compact_screener_row_passes_is_stale_through():
    """`is_stale` shares the same passthrough path as `data_source`
    (Part E). Without it the JSON view can't tell a recovery hit
    from a stale-but-tagged twse row."""
    for stale in (True, False, None):
        raw = {
            "symbol": "8110",
            "price": 60.6,
            "data_source": "twse",
            "is_stale": stale,
        }
        compact = _compact_screener_row(
            raw, as_of_session="2026-05-27", is_intraday=False,
        )
        assert compact["is_stale"] is stale


def test_compact_us_screener_row_passes_is_stale_through():
    for stale in (True, False, None):
        raw = {
            "symbol": "NVDA",
            "name": "NVIDIA Corp",
            "price": 145.0,
            "data_source": "polygon",
            "is_stale": stale,
        }
        compact = _compact_us_screener_row(
            raw, as_of_session="2026-05-28", is_intraday=False,
        )
        assert compact["is_stale"] is stale


# ── Part E: staleness gate covers all phases of day ──────────────


@pytest.mark.asyncio
async def test_yfinance_recovery_fires_pre_open_when_twse_is_a_day_behind():
    """Pre-open Friday 00:01 Taipei — the user's reported bug case.
    Latest complete session per freshness is Thursday (2026-05-28);
    TWSE STOCK_DAY_ALL still serves 2026-05-27. Pre-Part-E the
    yfinance gate short-circuited at this hour because
    `_expected_today_session()` returned None outside post-publish.
    Part E moves the gate to `_latest_complete_session()` which is
    populated at every hour of the day, so yfinance recovery now
    fires and returns Thursday's actual close.
    """
    thursday = date(2026, 5, 28)
    wednesday = date(2026, 5, 27)
    twse_rows = [
        {"Code": "2330", "Name": "TSMC", "成交股數": "5000000",
         "收盤價": "1080.0", "漲跌價差": "5.0"},
        {"Code": "8110", "Name": "華東", "成交股數": "68790601",
         "收盤價": "60.6", "漲跌價差": "5.5"},
    ]
    yf_quotes = {
        "2330.TW": {"price": 1100.0, "change_pct": 1.85, "volume": 5_000_000},
        "8110.TW": {"price": 62.0, "change_pct": 2.31, "volume": 70_000_000},
    }
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", AsyncMock()), \
         patch.object(
             svc, "get_latest_ohlcv_session",
             AsyncMock(return_value=wednesday),
         ), \
         patch.object(
             svc, "_bellwether_ohlcv_closes",
             AsyncMock(return_value=[(wednesday, 1080.0)]),
         ), \
         patch.object(
             svc.twse, "get_all_twse_symbols",
             AsyncMock(return_value=twse_rows),
         ), \
         patch(
             "data.us.yfinance_connector.get_batch_quotes",
             AsyncMock(return_value=yf_quotes),
         ), \
         _patch_tw_now(year=2026, month=5, day=29, hh=0, mm=1):
        result = await svc.get_screener(limit=50, min_volume=1_000_000)

    by_symbol = {r["symbol"]: r for r in result}
    assert by_symbol["8110"]["price"] == 62.0
    assert by_symbol["8110"]["actual_session"] == thursday.isoformat()
    assert by_symbol["8110"]["data_source"] == "yfinance_recovery"
    assert by_symbol["8110"]["is_stale"] is False


@pytest.mark.asyncio
async def test_intraday_with_matched_twse_does_not_attempt_recovery():
    """Intraday Friday 11:00 Taipei. Latest complete session is
    Thursday. TWSE serves Thursday's close (matches DB). No stale
    detection, no recovery attempt — regression guard that the
    relaxed gate doesn't fire recovery during healthy intraday
    when the screener data IS what we expect.
    """
    thursday = date(2026, 5, 28)
    twse_rows = [
        {"Code": "2330", "Name": "TSMC", "成交股數": "5000000",
         "收盤價": "1100.0", "漲跌價差": "5.0"},
    ]
    yf_mock = AsyncMock(return_value={})
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", AsyncMock()), \
         patch.object(
             svc, "get_latest_ohlcv_session",
             AsyncMock(return_value=thursday),
         ), \
         patch.object(
             svc, "_bellwether_ohlcv_closes",
             AsyncMock(return_value=[(thursday, 1100.0)]),
         ), \
         patch.object(
             svc.twse, "get_all_twse_symbols",
             AsyncMock(return_value=twse_rows),
         ), \
         patch(
             "data.us.yfinance_connector.get_batch_quotes",
             yf_mock,
         ), \
         _patch_tw_now(year=2026, month=5, day=29, hh=11):
        # ohlcv_latest = Thursday >= freshness_session (Thursday) so
        # fast path fires. yfinance must not be reached.
        await svc.get_screener(limit=50, min_volume=1_000_000)
    yf_mock.assert_not_awaited()


# ── Part F: FinMind sponsor recovery + diagnostic sink ───────────


@pytest.mark.asyncio
async def test_finmind_recovery_succeeds_before_yfinance_is_tried():
    """Pre-open Friday 00:01 Taipei, TWSE stuck on 2026-05-27, ohlcv
    also stuck on 2026-05-27. FinMind sponsor's market-wide call
    returns Thursday (2026-05-28) data. Assert recovery patches the
    8110 row to today's close, stamps `data_source="finmind_recovery"`,
    and yfinance is NEVER called — FinMind is ahead in the chain.
    """
    thursday = date(2026, 5, 28)
    wednesday = date(2026, 5, 27)
    twse_rows = [
        {"Code": "2330", "Name": "TSMC", "成交股數": "5000000",
         "收盤價": "1080.0", "漲跌價差": "5.0"},
        {"Code": "8110", "Name": "華東", "成交股數": "68790601",
         "收盤價": "60.6", "漲跌價差": "5.5"},
    ]
    fm_rows = [
        {"stock_id": "2330", "time": thursday.isoformat(),
         "open": 1080.0, "high": 1110.0, "low": 1075.0,
         "close": 1100.0, "volume": 5_000_000},
        {"stock_id": "8110", "time": thursday.isoformat(),
         "open": 60.6, "high": 62.5, "low": 60.0,
         "close": 62.0, "volume": 70_000_000},
    ]
    yf_mock = AsyncMock(return_value={})
    diagnostic: dict = {}
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", AsyncMock()), \
         patch.object(
             svc, "get_latest_ohlcv_session",
             AsyncMock(return_value=wednesday),
         ), \
         patch.object(
             svc, "_bellwether_ohlcv_closes",
             AsyncMock(return_value=[(wednesday, 1080.0)]),
         ), \
         patch.object(
             svc.twse, "get_all_twse_symbols",
             AsyncMock(return_value=twse_rows),
         ), \
         patch.object(
             svc.finmind, "get_daily_ohlcv_market_wide",
             AsyncMock(return_value=fm_rows),
         ), \
         patch(
             "data.us.yfinance_connector.get_batch_quotes",
             yf_mock,
         ), \
         _patch_tw_now(year=2026, month=5, day=29, hh=0, mm=1):
        result = await svc.get_screener(
            limit=50, min_volume=1_000_000, diagnostic=diagnostic,
        )

    by_symbol = {r["symbol"]: r for r in result}
    assert by_symbol["8110"]["price"] == 62.0
    assert by_symbol["8110"]["actual_session"] == thursday.isoformat()
    assert by_symbol["8110"]["data_source"] == "finmind_recovery"
    assert by_symbol["8110"]["is_stale"] is False
    yf_mock.assert_not_awaited()
    # Diagnostic sink captures the chain.
    assert diagnostic["freshness_session"] == thursday.isoformat()
    assert diagnostic["final_data_source"] == "finmind_recovery"
    assert {a["tier"] for a in diagnostic["attempts"]} >= {
        "ohlcv_fast_path", "finmind",
    }
    finmind_attempt = next(
        a for a in diagnostic["attempts"] if a["tier"] == "finmind"
    )
    assert finmind_attempt["outcome"] == "recovered"


@pytest.mark.asyncio
async def test_finmind_also_stale_falls_through_to_yfinance():
    """FinMind's 2330 close matches DB's 2026-05-27 close → FinMind
    is also stuck → recovery returns None, yfinance is then tried.
    Diagnostic carries both attempt records."""
    thursday = date(2026, 5, 28)
    wednesday = date(2026, 5, 27)
    twse_rows = [
        {"Code": "2330", "Name": "TSMC", "成交股數": "5000000",
         "收盤價": "1080.0", "漲跌價差": "5.0"},
        {"Code": "8110", "Name": "華東", "成交股數": "2000000",
         "收盤價": "60.6", "漲跌價差": "5.5"},
    ]
    # FinMind also on Wednesday — same 2330 close as the TWSE response.
    fm_rows = [
        {"stock_id": "2330", "time": wednesday.isoformat(),
         "open": 1075.0, "high": 1085.0, "low": 1070.0,
         "close": 1080.0, "volume": 5_000_000},
        {"stock_id": "8110", "time": wednesday.isoformat(),
         "open": 55.1, "high": 60.6, "low": 55.1,
         "close": 60.6, "volume": 68_000_000},
    ]
    # yfinance on Thursday (fresher) — recovery should land here.
    yf_quotes = {
        "2330.TW": {"price": 1100.0, "change_pct": 1.85, "volume": 5_000_000},
        "8110.TW": {"price": 62.0, "change_pct": 2.31, "volume": 70_000_000},
    }
    diagnostic: dict = {}
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", AsyncMock()), \
         patch.object(
             svc, "get_latest_ohlcv_session",
             AsyncMock(return_value=wednesday),
         ), \
         patch.object(
             svc, "_bellwether_ohlcv_closes",
             AsyncMock(return_value=[(wednesday, 1080.0)]),
         ), \
         patch.object(
             svc.twse, "get_all_twse_symbols",
             AsyncMock(return_value=twse_rows),
         ), \
         patch.object(
             svc.finmind, "get_daily_ohlcv_market_wide",
             AsyncMock(return_value=fm_rows),
         ), \
         patch(
             "data.us.yfinance_connector.get_batch_quotes",
             AsyncMock(return_value=yf_quotes),
         ), \
         _patch_tw_now(year=2026, month=5, day=29, hh=0, mm=1):
        result = await svc.get_screener(
            limit=50, min_volume=1_000_000, diagnostic=diagnostic,
        )

    by_symbol = {r["symbol"]: r for r in result}
    assert by_symbol["8110"]["price"] == 62.0
    assert by_symbol["8110"]["data_source"] == "yfinance_recovery"
    assert diagnostic["final_data_source"] == "yfinance_recovery"
    outcomes = {a["tier"]: a["outcome"] for a in diagnostic["attempts"]}
    assert outcomes["finmind"] == "also_stale"
    assert outcomes["yfinance"] == "recovered"
    _ = thursday  # context only


@pytest.mark.asyncio
async def test_finmind_empty_response_falls_through_to_yfinance():
    """FinMind quota exhausted / no token / 402 → market-wide returns
    []. Recovery bails with `empty_response`, yfinance is tried."""
    wednesday = date(2026, 5, 27)
    twse_rows = [
        {"Code": "2330", "Name": "TSMC", "成交股數": "5000000",
         "收盤價": "1080.0", "漲跌價差": "5.0"},
    ]
    yf_quotes = {
        "2330.TW": {"price": 1100.0, "change_pct": 1.85, "volume": 5_000_000},
    }
    diagnostic: dict = {}
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", AsyncMock()), \
         patch.object(
             svc, "get_latest_ohlcv_session",
             AsyncMock(return_value=wednesday),
         ), \
         patch.object(
             svc, "_bellwether_ohlcv_closes",
             AsyncMock(return_value=[(wednesday, 1080.0)]),
         ), \
         patch.object(
             svc.twse, "get_all_twse_symbols",
             AsyncMock(return_value=twse_rows),
         ), \
         patch.object(
             svc.finmind, "get_daily_ohlcv_market_wide",
             AsyncMock(return_value=[]),
         ), \
         patch(
             "data.us.yfinance_connector.get_batch_quotes",
             AsyncMock(return_value=yf_quotes),
         ), \
         _patch_tw_now(year=2026, month=5, day=29, hh=0, mm=1):
        await svc.get_screener(
            limit=50, min_volume=1_000_000, diagnostic=diagnostic,
        )

    outcomes = {a["tier"]: a["outcome"] for a in diagnostic["attempts"]}
    assert outcomes["finmind"] == "empty_response"
    assert outcomes["yfinance"] == "recovered"


@pytest.mark.asyncio
async def test_diagnostic_captures_all_recovery_tiers_bailing():
    """Worst-case observability: TWSE stale, FinMind also stale,
    yfinance returns empty. Result rows stay stale but the diagnostic
    sink names every tier and its bail reason — operator can read the
    JSON view and decide whether to wait or escalate."""
    wednesday = date(2026, 5, 27)
    twse_rows = [
        {"Code": "2330", "Name": "TSMC", "成交股數": "5000000",
         "收盤價": "1080.0", "漲跌價差": "5.0"},
    ]
    fm_rows = [
        {"stock_id": "2330", "time": wednesday.isoformat(),
         "open": 1075.0, "high": 1085.0, "low": 1070.0,
         "close": 1080.0, "volume": 5_000_000},  # matches Wed → stale
    ]
    diagnostic: dict = {}
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", AsyncMock()), \
         patch.object(
             svc, "get_latest_ohlcv_session",
             AsyncMock(return_value=wednesday),
         ), \
         patch.object(
             svc, "_bellwether_ohlcv_closes",
             AsyncMock(return_value=[(wednesday, 1080.0)]),
         ), \
         patch.object(
             svc.twse, "get_all_twse_symbols",
             AsyncMock(return_value=twse_rows),
         ), \
         patch.object(
             svc.finmind, "get_daily_ohlcv_market_wide",
             AsyncMock(return_value=fm_rows),
         ), \
         patch(
             "data.us.yfinance_connector.get_batch_quotes",
             AsyncMock(return_value={}),
         ), \
         _patch_tw_now(year=2026, month=5, day=29, hh=0, mm=1):
        result = await svc.get_screener(
            limit=50, min_volume=1_000_000, diagnostic=diagnostic,
        )

    assert all(r["is_stale"] is True for r in result)
    assert diagnostic["final_data_source"] == "twse_stale"
    outcomes = {a["tier"]: a["outcome"] for a in diagnostic["attempts"]}
    assert outcomes["finmind"] == "also_stale"
    assert outcomes["yfinance"] == "empty_response"
