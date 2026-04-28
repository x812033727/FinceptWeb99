"""Tests for tasks.ingest_ohlcv_tw — the daily TW OHLCV scheduler job.

Connectors are mocked; the in-memory SQLite test engine is reused via the
db_session fixture's underlying TestSessionLocal. The Redis lock is
mocked so we can exercise both the lock-acquired and lock-held paths.
"""
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.ohlcv_daily import OhlcvDaily


def _twse_row(d: str, close: float = 600.0) -> dict:
    return {
        "time": d, "open": close - 1, "high": close + 1, "low": close - 2,
        "close": close, "volume": 1_000,
    }


@pytest.fixture
def patch_session(db_session: AsyncSession):
    """Make AsyncSessionLocal() yield the shared test session.

    The ingest task opens its own session via `AsyncSessionLocal()`. We
    swap that with a context-manager around the per-test sqlite session
    so writes land in the same DB the test reads from.
    """
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch("tasks.ingest_ohlcv_tw.AsyncSessionLocal", return_value=_CM()):
        yield


@pytest.mark.asyncio
async def test_lock_held_skips_work(patch_session):
    from tasks import ingest_ohlcv_tw

    with patch("tasks.ingest_ohlcv_tw.acquire_lock", AsyncMock(return_value=False)), \
         patch("tasks.ingest_ohlcv_tw.release_lock", AsyncMock()) as release, \
         patch.object(ingest_ohlcv_tw, "_load_symbols", AsyncMock()) as load:
        await ingest_ohlcv_tw.run()

    load.assert_not_awaited()
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_symbols_records_failed_health(patch_session):
    from tasks import ingest_ohlcv_tw

    with patch("tasks.ingest_ohlcv_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_ohlcv_tw.release_lock", AsyncMock()), \
         patch.object(ingest_ohlcv_tw, "_load_symbols", AsyncMock(return_value=[])), \
         patch("tasks.ingest_ohlcv_tw.record_health", AsyncMock()) as health:
        await ingest_ohlcv_tw.run()

    health.assert_awaited_once()
    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["error"] == "no_symbols"


@pytest.mark.asyncio
async def test_twse_success_writes_bars(patch_session, db_session: AsyncSession):
    from tasks import ingest_ohlcv_tw

    with patch("tasks.ingest_ohlcv_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_ohlcv_tw.release_lock", AsyncMock()) as release, \
         patch.object(ingest_ohlcv_tw, "_load_symbols", AsyncMock(return_value=["OT_2330"])), \
         patch("tasks.ingest_ohlcv_tw.twse.get_daily_ohlcv",
               AsyncMock(return_value=[_twse_row("2026-04-01"), _twse_row("2026-04-02", 605)])), \
         patch("tasks.ingest_ohlcv_tw.finmind.get_daily_ohlcv", AsyncMock()) as finmind_mock, \
         patch("tasks.ingest_ohlcv_tw.record_health", AsyncMock()) as health:
        await ingest_ohlcv_tw.run()

    finmind_mock.assert_not_awaited()
    release.assert_awaited_once()

    rows = (await db_session.scalars(
        select(OhlcvDaily).where(OhlcvDaily.symbol == "OT_2330")
    )).all()
    assert {r.ts for r in rows} == {date(2026, 4, 1), date(2026, 4, 2)}
    assert all(r.source == "twse" for r in rows)

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 2


@pytest.mark.asyncio
async def test_twse_fail_falls_back_to_finmind(patch_session, db_session: AsyncSession):
    from tasks import ingest_ohlcv_tw

    with patch("tasks.ingest_ohlcv_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_ohlcv_tw.release_lock", AsyncMock()), \
         patch.object(ingest_ohlcv_tw, "_load_symbols", AsyncMock(return_value=["1101"])), \
         patch("tasks.ingest_ohlcv_tw.twse.get_daily_ohlcv",
               AsyncMock(side_effect=RuntimeError("twse blocked"))), \
         patch("tasks.ingest_ohlcv_tw.finmind.get_daily_ohlcv",
               AsyncMock(return_value=[_twse_row("2026-04-01", 50.0)])), \
         patch("tasks.ingest_ohlcv_tw.record_health", AsyncMock()):
        await ingest_ohlcv_tw.run()

    rows = (await db_session.scalars(
        select(OhlcvDaily).where(OhlcvDaily.symbol == "1101")
    )).all()
    assert len(rows) == 1
    assert rows[0].source == "finmind"


@pytest.mark.asyncio
async def test_per_symbol_failure_does_not_abort_run(patch_session, db_session: AsyncSession):
    """One bad symbol must not stop ingest of the next."""
    from tasks import ingest_ohlcv_tw

    async def twse_side_effect(symbol, _today):
        if symbol == "BAD":
            raise RuntimeError("boom")
        return [_twse_row("2026-04-01", 100.0)]

    with patch("tasks.ingest_ohlcv_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_ohlcv_tw.release_lock", AsyncMock()), \
         patch.object(ingest_ohlcv_tw, "_load_symbols",
                      AsyncMock(return_value=["BAD", "GOOD"])), \
         patch("tasks.ingest_ohlcv_tw.twse.get_daily_ohlcv",
               AsyncMock(side_effect=twse_side_effect)), \
         patch("tasks.ingest_ohlcv_tw.finmind.get_daily_ohlcv",
               AsyncMock(side_effect=RuntimeError("also boom"))), \
         patch("tasks.ingest_ohlcv_tw.record_health", AsyncMock()) as health:
        await ingest_ohlcv_tw.run()

    rows = (await db_session.scalars(
        select(OhlcvDaily).where(OhlcvDaily.symbol == "GOOD")
    )).all()
    assert len(rows) == 1

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 1
