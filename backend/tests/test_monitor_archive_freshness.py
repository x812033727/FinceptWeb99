"""Content-level freshness: read max(ts) per dataset table instead of
trusting job health records, which have reported `failed` on runs that
wrote 15k rows and `ok` on runs that wrote none."""
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.ohlcv_daily import OhlcvDaily
from models.tw_chip_metrics import TwInstitutionalDaily, TwMarginDaily
from tasks import monitor_archive_freshness as task
from tasks.monitor_archive_freshness import _collect_latest, stale_datasets


def test_fresh_archive_reports_nothing():
    latest = {
        "ohlcv_tw": date(2026, 7, 24),
        "taiex": date(2026, 7, 24),
        "institutional_tw": date(2026, 7, 24),
        "margin_tw": date(2026, 7, 24),
    }
    assert stale_datasets(latest, expected=date(2026, 7, 24)) == []


def test_lagging_dataset_is_named_with_its_lag():
    latest = {
        "ohlcv_tw": date(2026, 7, 24),
        "taiex": date(2026, 7, 24),
        "institutional_tw": date(2026, 7, 21),   # 3 days behind
        "margin_tw": date(2026, 7, 24),
    }
    stale = stale_datasets(latest, expected=date(2026, 7, 24))
    assert stale == ["institutional_tw: 2026-07-21 (expected 2026-07-24)"]


def test_empty_table_is_stale_not_a_crash():
    latest = {"ohlcv_tw": None}
    stale = stale_datasets(latest, expected=date(2026, 7, 24))
    assert stale == ["ohlcv_tw: empty (expected 2026-07-24)"]


@pytest.mark.asyncio
async def test_collect_latest_excludes_underscore_symbols_from_ohlcv_tw(
    db_session: AsyncSession,
):
    """Regression for the LIKE-wildcard escaping bug: `_` is the SQL
    single-char wildcard, so an unescaped `NOT LIKE '_%'` excluded
    every non-empty symbol and `ohlcv_tw` was always None.

    `_TAIEX` is made the NEWEST row on purpose: if the underscore-
    prefixed index symbol leaks into the `ohlcv_tw` aggregate (the
    bug's failure mode, or the no-escape "fix" that still doesn't
    filter anything on SQLite), `ohlcv_tw` comes back 2026-07-24
    instead of the true stock-only max of 2026-07-23.
    """
    db_session.add_all([
        OhlcvDaily(
            market="TW", symbol="2330", ts=date(2026, 7, 23),
            open=10, high=10, low=10, close=10, volume=100, source="test",
        ),
        OhlcvDaily(
            market="TW", symbol="_TAIEX", ts=date(2026, 7, 24),
            open=10, high=10, low=10, close=10, volume=100, source="test",
        ),
    ])
    await db_session.flush()

    latest = await _collect_latest(db_session)

    assert latest["ohlcv_tw"] == date(2026, 7, 23)
    assert latest["taiex"] == date(2026, 7, 24)


@pytest.mark.asyncio
async def test_run_stamps_oldest_observed_not_desired_session(
    db_session: AsyncSession,
):
    """`latest_data_ts` must reflect what was actually found (the
    oldest observed dataset), not `expected` — stamping the desired
    session would show a fresh-looking timestamp on the admin
    dashboard right beside `ok=False`."""
    from datetime import datetime as real_datetime
    from zoneinfo import ZoneInfo

    # 2026-07-24 is a Friday; prev_trading_day_estimate walks back to
    # Thursday 2026-07-23 with no weekend to skip.
    fixed_now = real_datetime(2026, 7, 24, 3, 0, tzinfo=ZoneInfo("Asia/Taipei"))

    db_session.add_all([
        OhlcvDaily(market="TW", symbol="2330", ts=date(2026, 7, 23),
                   open=1, high=1, low=1, close=1, volume=1, source="test"),
        OhlcvDaily(market="TW", symbol="_TAIEX", ts=date(2026, 7, 23),
                   open=1, high=1, low=1, close=1, volume=1, source="test"),
        # 2 trading days behind — makes this run ok=False.
        TwInstitutionalDaily(market="TW", symbol="2330", ts=date(2026, 7, 21),
                              source="test"),
        TwMarginDaily(market="TW", symbol="2330", ts=date(2026, 7, 23),
                      source="test"),
    ])
    await db_session.commit()

    class _FixedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    with patch.object(task, "datetime", _FixedDatetime), \
         patch.object(task, "record_health", AsyncMock()) as health:
        await task.run()

    assert health.await_count == 1
    kwargs = health.call_args.kwargs
    assert kwargs["ok"] is False
    # Oldest observed date across the four datasets — NOT `expected`
    # (2026-07-23), which would have been the old (wrong) behavior.
    assert kwargs["latest_data_ts"] == date(2026, 7, 21)
