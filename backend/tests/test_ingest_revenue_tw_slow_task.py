"""Tests for `tasks.ingest_revenue_tw_slow` — the per-symbol MOPS
fan-out cron that took over the FinMind market-wide cron after the
2026-04 paywall.

Cursor advance, partial-failure tolerance, wrap-around cycle counter,
empty-universe pending-state handling are all pinned here.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.tw_revenue_monthly import TwRevenueMonthly


# ── helpers ───────────────────────────────────────────────────────

@pytest.fixture
def patch_session(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch(
        "tasks.ingest_revenue_tw_slow.AsyncSessionLocal",
        return_value=_CM(),
    ):
        yield


def _row(symbol: str = "2330", date_str: str = "2025-04-01",
         revenue: int = 1_234_567, yoy: float = 12.5,
         mom: float = -3.2) -> dict:
    return {
        "symbol": symbol,
        "date": date_str,
        "revenue": revenue,
        "revenue_yoy": yoy,
        "revenue_mom": mom,
    }


def _patch_universe(symbols: list[str]):
    """Snapshot the in-memory `_exchange_map` to a fixed list. The
    cron's `_list_symbols` reads from the live module, so we patch
    the reference."""
    return patch.dict(
        "services.tw_market_service._exchange_map",
        {s: "TWSE" for s in symbols},
        clear=True,
    )


def _common_patches(*, cursor_value: str | None = None):
    return [
        patch("tasks.ingest_revenue_tw_slow.acquire_lock",
              AsyncMock(return_value=True)),
        patch("tasks.ingest_revenue_tw_slow.release_lock", AsyncMock()),
        patch("tasks.ingest_revenue_tw_slow.backoff_remaining_seconds",
              AsyncMock(return_value=0)),
        patch("tasks.ingest_revenue_tw_slow.cache_get",
              AsyncMock(return_value=cursor_value)),
        patch("tasks.ingest_revenue_tw_slow.cache_set", AsyncMock()),
        patch("tasks.ingest_revenue_tw_slow.cache_incr",
              AsyncMock(return_value=1)),
        patch("tasks.ingest_revenue_tw_slow.clear_failures", AsyncMock()),
    ]


@pytest.fixture(autouse=True)
def _no_real_sleep():
    """The cron paces ~5 s between symbols. Fast-forward those in
    tests so the suite stays sub-second."""
    with patch("tasks.ingest_revenue_tw_slow.asyncio.sleep", AsyncMock()):
        yield


# ── tests ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lock_held_skips_work(patch_session):
    from tasks import ingest_revenue_tw_slow

    fetch_mock = AsyncMock()
    with patch("tasks.ingest_revenue_tw_slow.acquire_lock",
               AsyncMock(return_value=False)), \
         patch("tasks.ingest_revenue_tw_slow.release_lock", AsyncMock()), \
         patch("tasks.ingest_revenue_tw_slow.mops.get_monthly_revenue_recent",
               fetch_mock):
        await ingest_revenue_tw_slow.run()

    fetch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_processes_batch_starting_at_cursor(
    patch_session, db_session: AsyncSession,
):
    """Cron should start at the cursor value and process up to
    BATCH_SIZE symbols."""
    from tasks import ingest_revenue_tw_slow

    universe = [f"S{i:04d}" for i in range(200)]
    fetch_mock = AsyncMock(return_value=[_row()])
    health_mock = AsyncMock()
    cache_set_mock = AsyncMock()

    patches = _common_patches(cursor_value="50") + [
        patch("tasks.ingest_revenue_tw_slow.cache_set", cache_set_mock),
        patch("tasks.ingest_revenue_tw_slow.record_health", health_mock),
        patch("tasks.ingest_revenue_tw_slow.mops.get_monthly_revenue_recent",
              fetch_mock),
        _patch_universe(universe),
    ]
    for p in patches:
        p.__enter__()
    try:
        await ingest_revenue_tw_slow.run()
    finally:
        for p in reversed(patches):
            p.__exit__(None, None, None)

    # 75 symbols starting from index 50 → calls for S0050..S0124
    called_symbols = [c.args[0] for c in fetch_mock.await_args_list]
    assert len(called_symbols) == 75
    assert called_symbols[0] == "S0050"
    assert called_symbols[-1] == "S0124"

    # Cursor advances to 125, not wrapped (50 + 75 < 200).
    cursor_writes = [c for c in cache_set_mock.await_args_list
                     if c.args[0] == "ingest:revenue_tw_slow:cursor"]
    assert cursor_writes
    assert cursor_writes[-1].args[1] == "125"

    # Health: ok=True, slice/total reflected in status text.
    health_mock.assert_awaited_once()
    kwargs = health_mock.await_args.kwargs
    assert kwargs["ok"] is True
    assert "slice 50-125/200" in kwargs["error"]


@pytest.mark.asyncio
async def test_per_symbol_failure_does_not_abort_batch(patch_session):
    """One bad symbol = one logged warning, batch continues."""
    from tasks import ingest_revenue_tw_slow

    universe = [f"S{i:04d}" for i in range(10)]

    async def flaky(symbol: str):
        if symbol == "S0003":
            raise RuntimeError("upstream chaos")
        return [_row(symbol=symbol)]

    fetch_mock = AsyncMock(side_effect=flaky)
    health_mock = AsyncMock()
    patches = _common_patches(cursor_value="0") + [
        patch("tasks.ingest_revenue_tw_slow.record_health", health_mock),
        patch("tasks.ingest_revenue_tw_slow.mops.get_monthly_revenue_recent",
              fetch_mock),
        _patch_universe(universe),
    ]
    for p in patches:
        p.__enter__()
    try:
        await ingest_revenue_tw_slow.run()
    finally:
        for p in reversed(patches):
            p.__exit__(None, None, None)

    # Every symbol attempted, not just the ones before the failure.
    assert fetch_mock.await_count == 10
    # Health summary surfaces the transient-error count.
    kwargs = health_mock.await_args.kwargs
    assert kwargs["ok"] is True
    assert "1 transient errors" in kwargs["error"]
    assert "9/10 returned data" in kwargs["error"]


@pytest.mark.asyncio
async def test_cursor_wraps_at_universe_end_and_bumps_cycle(patch_session):
    """When the batch reaches the end of the universe, cursor resets
    to 0 and the cycle counter bumps so admins can see how many full
    sweeps have happened."""
    from tasks import ingest_revenue_tw_slow

    universe = [f"S{i:04d}" for i in range(80)]
    fetch_mock = AsyncMock(return_value=[_row()])
    cache_set_mock = AsyncMock()
    cache_incr_mock = AsyncMock(return_value=3)
    health_mock = AsyncMock()
    patches = _common_patches(cursor_value="20") + [
        patch("tasks.ingest_revenue_tw_slow.cache_set", cache_set_mock),
        patch("tasks.ingest_revenue_tw_slow.cache_incr", cache_incr_mock),
        patch("tasks.ingest_revenue_tw_slow.record_health", health_mock),
        patch("tasks.ingest_revenue_tw_slow.mops.get_monthly_revenue_recent",
              fetch_mock),
        _patch_universe(universe),
    ]
    for p in patches:
        p.__enter__()
    try:
        await ingest_revenue_tw_slow.run()
    finally:
        for p in reversed(patches):
            p.__exit__(None, None, None)

    # 20 + 75 = 95 > universe size 80, so processed S0020..S0079 (60 sym)
    # then cursor wraps to 0.
    assert fetch_mock.await_count == 60
    cursor_writes = [c for c in cache_set_mock.await_args_list
                     if c.args[0] == "ingest:revenue_tw_slow:cursor"]
    assert cursor_writes[-1].args[1] == "0"
    cache_incr_mock.assert_awaited_once()
    assert "cycle #3 complete" in health_mock.await_args.kwargs["error"]


@pytest.mark.asyncio
async def test_empty_universe_yields_pending_status(patch_session):
    """If the symbol-map cron hasn't run yet (fresh deploy), the
    universe is empty — cron should record an informative `skipped`
    health and NOT arm backoff."""
    from tasks import ingest_revenue_tw_slow

    fetch_mock = AsyncMock()
    record_failure_mock = AsyncMock()
    health_mock = AsyncMock()
    patches = _common_patches() + [
        patch("tasks.ingest_revenue_tw_slow.record_failure",
              record_failure_mock),
        patch("tasks.ingest_revenue_tw_slow.record_health", health_mock),
        patch("tasks.ingest_revenue_tw_slow.mops.get_monthly_revenue_recent",
              fetch_mock),
        _patch_universe([]),
    ]
    for p in patches:
        p.__enter__()
    try:
        await ingest_revenue_tw_slow.run()
    finally:
        for p in reversed(patches):
            p.__exit__(None, None, None)

    fetch_mock.assert_not_called()
    record_failure_mock.assert_not_called()
    kwargs = health_mock.await_args.kwargs
    assert kwargs["ok"] is True
    assert "symbol map not loaded" in kwargs["error"].lower()


@pytest.mark.asyncio
async def test_writes_rows_to_db(
    patch_session, db_session: AsyncSession,
):
    """End-to-end: a successful tick actually upserts into
    `tw_revenue_monthly`."""
    from tasks import ingest_revenue_tw_slow

    universe = ["2330"]
    fetch_mock = AsyncMock(return_value=[
        _row(symbol="2330", date_str="2025-04-01",
             revenue=1_000_000, yoy=12.5, mom=-3.2),
        _row(symbol="2330", date_str="2025-03-01",
             revenue=950_000, yoy=10.0, mom=2.5),
    ])
    patches = _common_patches() + [
        patch("tasks.ingest_revenue_tw_slow.record_health", AsyncMock()),
        patch("tasks.ingest_revenue_tw_slow.mops.get_monthly_revenue_recent",
              fetch_mock),
        _patch_universe(universe),
    ]
    for p in patches:
        p.__enter__()
    try:
        await ingest_revenue_tw_slow.run()
    finally:
        for p in reversed(patches):
            p.__exit__(None, None, None)

    db_rows = (await db_session.scalars(
        select(TwRevenueMonthly).where(TwRevenueMonthly.symbol == "2330")
    )).all()
    assert len(db_rows) == 2
    assert {int(r.revenue) for r in db_rows} == {1_000_000, 950_000}
    assert all(r.source == "mops" for r in db_rows)
