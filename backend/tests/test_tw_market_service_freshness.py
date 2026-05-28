"""Phase 3 tests for `tw_market_service` freshness gates.

Three gates close the "DB returned yesterday because the ingest cron
hasn't fired yet" hole:

  1. `_db_bars_are_fresh` — tightened from 5-day to "last bar must
     cover the expected session" so stale archives fall through to
     TWSE STOCK_DAY instead of being served as fresh.

  2. `get_history` (TW) — calls the tightened gate; verified
     via integration that a stale DB falls through to TWSE and
     today's bar lands.

  3. `get_index` (TW live) — splices today's TAIEX close from
     `MI_5MINS` (already in the quote payload) when the
     `ohlcv_daily._TAIEX` archive is missing today's bar, so the
     30-day history line ends on today instead of yesterday during
     the post-publish / pre-cron window (~13:30-15:10 Taipei).
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest
import pytz

from services import tw_market_service as svc


_TW = pytz.timezone("Asia/Taipei")


def _patch_tw_now(*, year: int, month: int, day: int, hh: int, mm: int = 0):
    """Replace `datetime.now(UTC)` in BOTH freshness modules so phase
    classification AND `utcnow_tw_date()` (consumed by `get_index`'s
    history range) align to the simulated Taipei wall-clock.

    Returns a context manager that activates both patches. The
    freshness helper's `_tw_latest_complete_session` reads `now=` via
    `datetime.now(UTC).astimezone(Asia/Taipei)`; `utcnow_tw_date()`
    in `tw_trading_calendar` does the same. Patching the `datetime`
    symbol in each module keeps every call path deterministic.
    """
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
        patch("services.tw_trading_calendar.datetime", _FrozenDatetime),
    )
    return stack


# ── _db_bars_are_fresh / _expected_history_session ────────────────


def test_db_bars_are_fresh_accepts_when_last_bar_is_expected_session():
    """During TWSE pre-publish (e.g. 11:00 Taipei Wed), the expected
    session is Tuesday (STOCK_DAY_ALL hasn't refreshed). A DB whose
    last bar is Tuesday is correctly fresh — no need to fall through.
    """
    bars = [{"time": "2026-05-26", "close": 1090}]  # Tue
    with _patch_tw_now(year=2026, month=5, day=27, hh=11):  # Wed 11:00
        assert svc._db_bars_are_fresh(bars, today=date(2026, 5, 27)) is True


def test_db_bars_are_fresh_rejects_when_last_bar_predates_expected_session():
    """Post-publish (>= 14:30 Taipei) but `ingest_ohlcv_tw` is
    delayed: expected session = today (Wed), DB still at Tuesday.
    Old 5-day tolerance let this pass silently. Tightened gate
    must reject so the caller falls through to TWSE STOCK_DAY for
    today's bar.
    """
    bars = [{"time": "2026-05-26", "close": 1090}]  # Tue
    with _patch_tw_now(year=2026, month=5, day=27, hh=15):  # Wed 15:00
        assert svc._db_bars_are_fresh(bars, today=date(2026, 5, 27)) is False


def test_db_bars_are_fresh_accepts_when_today_bar_is_present_post_publish():
    """The happy path — cron ran on time, today's bar in DB, expected
    session is today. Pass."""
    bars = [{"time": "2026-05-27", "close": 1095}]
    with _patch_tw_now(year=2026, month=5, day=27, hh=15):
        assert svc._db_bars_are_fresh(bars, today=date(2026, 5, 27)) is True


def test_db_bars_are_fresh_handles_weekend_anchor_correctly():
    """On Saturday, the expected session = Friday (most recent
    weekday). A DB whose last bar is Friday passes; a DB stuck on
    Thursday fails — same tolerance shape as weekdays.
    """
    fri = [{"time": "2026-05-29", "close": 1100}]  # Fri
    thu = [{"time": "2026-05-28", "close": 1095}]  # Thu (stale)
    with _patch_tw_now(year=2026, month=5, day=30, hh=10):  # Sat 10:00
        assert svc._db_bars_are_fresh(fri, today=date(2026, 5, 30)) is True
        assert svc._db_bars_are_fresh(thu, today=date(2026, 5, 30)) is False


def test_db_bars_are_fresh_empty_or_invalid():
    """Empty bars + non-ISO timestamps must return False — a False
    result kicks the caller into the upstream fallback path, which
    is the right move when the archive is broken.
    """
    assert svc._db_bars_are_fresh([], today=date.today()) is False
    assert svc._db_bars_are_fresh(
        [{"time": "garbage"}], today=date.today(),
    ) is False
    assert svc._db_bars_are_fresh(
        [{"close": 100}], today=date.today(),
    ) is False


# ── get_history fallthrough ────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_history_falls_through_to_twse_when_db_stale():
    """Phase 3 integration check: stale DB (Tuesday) + post-publish
    Wednesday + working TWSE → today's bar lands via STOCK_DAY
    waterfall. Before tightening the gate, the stale DB was treated
    as fresh and personas saw yesterday's close as today's.
    """
    stale_db = [
        {"time": "2026-05-22", "open": 1075, "high": 1080, "low": 1070,
         "close": 1078, "volume": 1000},
        {"time": "2026-05-26", "open": 1080, "high": 1095, "low": 1075,
         "close": 1090, "volume": 1500},  # Tuesday's bar — stale
    ]
    twse_month = [
        {"time": "2026-05-26", "open": 1080, "high": 1095, "low": 1075,
         "close": 1090, "volume": 1500},
        {"time": "2026-05-27", "open": 1095, "high": 1110, "low": 1090,
         "close": 1105, "volume": 2000},  # Today
    ]
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch(
             "services.ingest.repository.read_ohlcv_range_autosession",
             AsyncMock(return_value=stale_db),
         ), \
         patch(
             "services.ingest.repository.upsert_ohlcv_bars_autosession",
             AsyncMock(return_value=2),
         ), \
         patch.object(
             svc.twse, "get_daily_ohlcv",
             AsyncMock(return_value=twse_month),
         ), \
         _patch_tw_now(year=2026, month=5, day=27, hh=15):
        bars = await svc.get_history("2330", months=1)

    # The fall-through returned the TWSE-fresh series including
    # today (2026-05-27), not the stale DB's last bar at 2026-05-26.
    assert bars[-1]["time"] == "2026-05-27"
    assert bars[-1]["close"] == 1105


@pytest.mark.asyncio
async def test_get_history_uses_db_when_archive_is_fresh():
    """Counterpart: fresh DB (today already ingested) → no
    fallthrough, no TWSE call burned. Guards against accidentally
    flipping the gate the wrong way and DDoSing TWSE on every read.
    """
    fresh_db = [
        {"time": "2026-05-26", "open": 1080, "high": 1095, "low": 1075,
         "close": 1090, "volume": 1500},
        {"time": "2026-05-27", "open": 1095, "high": 1110, "low": 1090,
         "close": 1105, "volume": 2000},
    ]
    twse_called = AsyncMock(return_value=[])  # poison — must NOT fire
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch(
             "services.ingest.repository.read_ohlcv_range_autosession",
             AsyncMock(return_value=fresh_db),
         ), \
         patch.object(svc.twse, "get_daily_ohlcv", twse_called), \
         _patch_tw_now(year=2026, month=5, day=27, hh=15):
        bars = await svc.get_history("2330", months=1)

    twse_called.assert_not_awaited()
    assert bars[-1]["time"] == "2026-05-27"


# ── get_index splices today's TAIEX when archive is missing it ────


@pytest.mark.asyncio
async def test_get_index_splices_today_when_archive_missing_today_bar():
    """During the 13:30-15:10 Taipei window (TWSE published, but
    `ingest_taiex_history` cron at 07:10 UTC hasn't run yet), the
    `_TAIEX` archive ends YESTERDAY while `MI_5MINS` already has
    today's final close. The history line must end on today, not
    yesterday — otherwise the 「TAIEX 30 日線」 in the discussion ctx
    persistently lags one session behind reality.
    """
    archive_no_today = [
        {"time": f"2026-05-{20 + i:02d}", "close": 22000 + i * 10}
        for i in range(7)  # 2026-05-20 ... 2026-05-26 (Tuesday)
    ]
    taiex_live = {
        "index": "TAIEX", "value": 22150.5,
        "change": 80.0, "time": "13:30:00",
    }
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc.twse, "get_taiex", AsyncMock(return_value=taiex_live)), \
         patch(
             "services.ingest.repository.read_ohlcv_range_autosession",
             AsyncMock(return_value=archive_no_today),
         ), \
         _patch_tw_now(year=2026, month=5, day=27, hh=15):  # Wed 15:00
        out = await svc.get_index(history_days=30)

    history = out["history"]
    assert history[-1]["time"] == "2026-05-27", history
    assert history[-1]["close"] == 22150.5


@pytest.mark.asyncio
async def test_get_index_does_not_splice_on_weekend():
    """`MI_5MINS` caches the most recent session's last value, so on
    Saturday `value` is Friday's close. Splicing "Saturday bar =
    Friday's close" would mis-stamp the timeline. The weekday guard
    must drop the splice in this case.
    """
    archive_fri = [
        {"time": "2026-05-29", "close": 22100},  # Fri close
    ]
    taiex_live = {
        "index": "TAIEX", "value": 22100,  # MI_5MINS reuses Fri close
        "change": 0, "time": "13:30:00",
    }
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc.twse, "get_taiex", AsyncMock(return_value=taiex_live)), \
         patch(
             "services.ingest.repository.read_ohlcv_range_autosession",
             AsyncMock(return_value=archive_fri),
         ), \
         _patch_tw_now(year=2026, month=5, day=30, hh=10):  # Sat 10:00
        out = await svc.get_index(history_days=30)

    # No phantom Saturday bar.
    history = out["history"]
    assert all(
        date.fromisoformat(b["time"]).weekday() < 5 for b in history
    ), [b["time"] for b in history]


@pytest.mark.asyncio
async def test_get_index_does_not_splice_when_archive_already_has_today():
    """Happy path: cron already populated today's `_TAIEX` bar.
    The splice must NOT re-add a duplicate (or worse, overwrite the
    archive's authoritative OHLC close with the live MI_5MINS value).
    """
    archive_with_today = [
        {"time": "2026-05-26", "close": 22000},
        {"time": "2026-05-27", "close": 22150},
    ]
    taiex_live = {
        "index": "TAIEX", "value": 22150,
        "change": 150, "time": "13:30:00",
    }
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc.twse, "get_taiex", AsyncMock(return_value=taiex_live)), \
         patch(
             "services.ingest.repository.read_ohlcv_range_autosession",
             AsyncMock(return_value=archive_with_today),
         ), \
         _patch_tw_now(year=2026, month=5, day=27, hh=15):
        out = await svc.get_index(history_days=30)

    # Last bar is the archive's authoritative one, not a splice.
    history = out["history"]
    times = [b["time"] for b in history]
    assert times.count("2026-05-27") == 1
