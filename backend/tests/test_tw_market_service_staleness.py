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

from services import tw_market_service as svc
from services.discussion.context.builder import _maybe_downgrade_captured_session


_TW = pytz.timezone("Asia/Taipei")


def _patch_tw_now(*, year: int, month: int, day: int, hh: int, mm: int = 0):
    """Freeze both freshness clocks to a single Taipei wall-clock so
    `_expected_today_session` returns deterministic values."""
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


# ── _expected_today_session ───────────────────────────────────────


def test_expected_today_session_returns_today_post_publish():
    """Wed 15:00 Taipei is past 14:30 STOCK_DAY_ALL publish, so the
    expected "today's close" session is today's date."""
    with _patch_tw_now(year=2026, month=5, day=27, hh=15):
        assert svc._expected_today_session() == date(2026, 5, 27)


def test_expected_today_session_returns_none_pre_publish():
    """Wed 11:00 Taipei is intraday; STOCK_DAY_ALL hasn't refreshed
    so there is no "today's close" to expect yet."""
    with _patch_tw_now(year=2026, month=5, day=27, hh=11):
        assert svc._expected_today_session() is None


def test_expected_today_session_returns_none_on_weekend():
    """Sat 10:00 Taipei is non-trading; the freshness phase is
    `weekend_or_holiday`, not `today_close_published`."""
    with _patch_tw_now(year=2026, month=5, day=30, hh=10):
        assert svc._expected_today_session() is None


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
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
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
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
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
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
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
