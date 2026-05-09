"""Tests for tasks.ingest_fundamentals_tw — the daily TW fundamentals job."""
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.fundamentals_snapshot import FundamentalsSnapshot


@pytest.fixture
def patch_session(db_session: AsyncSession):
    """Make AsyncSessionLocal() yield the per-test sqlite session."""
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch("tasks.ingest_fundamentals_tw.AsyncSessionLocal", return_value=_CM()):
        yield


@pytest.fixture
def no_backoff():
    """Short-circuit Redis-backed backoff helpers so tests reach `_do_run`."""
    with patch(
        "tasks.ingest_fundamentals_tw.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.ingest_fundamentals_tw.clear_failures", AsyncMock(),
    ), patch(
        "tasks.ingest_fundamentals_tw.record_failure",
        AsyncMock(return_value=1),
    ):
        yield


@pytest.mark.asyncio
async def test_lock_held_skips_work(patch_session):
    from tasks import ingest_fundamentals_tw

    with patch("tasks.ingest_fundamentals_tw.acquire_lock", AsyncMock(return_value=False)), \
         patch("tasks.ingest_fundamentals_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_fundamentals_tw.twse.get_all_valuation_ratios",
               AsyncMock()) as twse_mock:
        await ingest_fundamentals_tw.run()

    twse_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_twse_failure_records_unhealthy_with_backoff(patch_session, no_backoff):
    from tasks import ingest_fundamentals_tw

    with patch("tasks.ingest_fundamentals_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_fundamentals_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_fundamentals_tw.twse.get_all_valuation_ratios",
               AsyncMock(side_effect=RuntimeError("twse 503"))), \
         patch("tasks.ingest_fundamentals_tw.record_health", AsyncMock()) as health:
        await ingest_fundamentals_tw.run()

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is False
    assert "auto-backoff armed" in kwargs["error"]


@pytest.mark.asyncio
async def test_empty_result_records_ok_with_zero_rows(patch_session, no_backoff):
    """An empty BWIBBU_ALL is a benign "no data published yet" — record
    ok=True row_count=0 instead of treating it as a failure."""
    from tasks import ingest_fundamentals_tw

    with patch("tasks.ingest_fundamentals_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_fundamentals_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_fundamentals_tw.twse.get_all_valuation_ratios",
               AsyncMock(return_value={})), \
         patch("tasks.ingest_fundamentals_tw.record_health", AsyncMock()) as health:
        await ingest_fundamentals_tw.run()

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 0


@pytest.mark.asyncio
async def test_success_writes_one_row_per_symbol(
    patch_session, no_backoff, db_session: AsyncSession,
):
    from tasks import ingest_fundamentals_tw

    canned = {
        "FUN_T1": {"pe_ratio": 15.0, "pb_ratio": 2.5, "dividend_yield": 4.0},
        "FUN_T2": {"pe_ratio": 22.0, "pb_ratio": 3.1, "dividend_yield": 1.8},
    }

    with patch("tasks.ingest_fundamentals_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_fundamentals_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_fundamentals_tw.twse.get_all_valuation_ratios",
               AsyncMock(return_value=canned)), \
         patch("tasks.ingest_fundamentals_tw.record_health", AsyncMock()) as health:
        await ingest_fundamentals_tw.run()

    rows = (await db_session.scalars(
        select(FundamentalsSnapshot)
        .where(FundamentalsSnapshot.symbol.in_(["FUN_T1", "FUN_T2"]))
    )).all()
    assert {r.symbol for r in rows} == {"FUN_T1", "FUN_T2"}
    assert {r.as_of for r in rows} == {date.today()}
    assert all(r.source == "twse" for r in rows)

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 2
    assert kwargs["latest_data_ts"] == date.today()


@pytest.mark.asyncio
async def test_rerun_same_day_overwrites(
    patch_session, no_backoff, db_session: AsyncSession,
):
    """Idempotent ingest — running twice must not produce two rows per symbol."""
    from tasks import ingest_fundamentals_tw

    canned_first = {"FUN_T3": {"pe_ratio": 15.0, "pb_ratio": 2.5, "dividend_yield": 4.0}}
    canned_second = {"FUN_T3": {"pe_ratio": 16.0, "pb_ratio": 2.6, "dividend_yield": 4.2}}

    common_patches = (
        patch("tasks.ingest_fundamentals_tw.acquire_lock", AsyncMock(return_value=True)),
        patch("tasks.ingest_fundamentals_tw.release_lock", AsyncMock()),
        patch("tasks.ingest_fundamentals_tw.record_health", AsyncMock()),
    )
    for ctx in common_patches:
        ctx.__enter__()
    try:
        with patch("tasks.ingest_fundamentals_tw.twse.get_all_valuation_ratios",
                   AsyncMock(return_value=canned_first)):
            await ingest_fundamentals_tw.run()
        with patch("tasks.ingest_fundamentals_tw.twse.get_all_valuation_ratios",
                   AsyncMock(return_value=canned_second)):
            await ingest_fundamentals_tw.run()
    finally:
        for ctx in common_patches:
            ctx.__exit__(None, None, None)

    rows = (await db_session.scalars(
        select(FundamentalsSnapshot).where(FundamentalsSnapshot.symbol == "FUN_T3")
    )).all()
    assert len(rows) == 1
    assert float(rows[0].pe_ratio) == 16.0


@pytest.mark.asyncio
async def test_backoff_active_skips_run(patch_session):
    """An armed backoff window short-circuits before any TWSE call."""
    from tasks import ingest_fundamentals_tw

    with patch("tasks.ingest_fundamentals_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_fundamentals_tw.release_lock", AsyncMock()), \
         patch(
             "tasks.ingest_fundamentals_tw.backoff_remaining_seconds",
             AsyncMock(return_value=3600),
         ), \
         patch(
             "tasks.ingest_fundamentals_tw.get_failure_count",
             AsyncMock(return_value=2),
         ), \
         patch(
             "tasks.ingest_fundamentals_tw.get_health", AsyncMock(return_value=None),
         ), \
         patch("tasks.ingest_fundamentals_tw.twse.get_all_valuation_ratios",
               AsyncMock()) as twse_mock, \
         patch("tasks.ingest_fundamentals_tw.record_health", AsyncMock()) as health:
        await ingest_fundamentals_tw.run()

    twse_mock.assert_not_awaited()
    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is False
    assert "skipped" in kwargs["error"]
    assert "backoff after 2 failures" in kwargs["error"]
