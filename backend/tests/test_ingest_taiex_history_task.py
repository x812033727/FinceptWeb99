"""Tests for tasks.ingest_taiex_history.

Pulls the current month's TAIEX FMTQIK series from TWSE and upserts
into ohlcv_daily under symbol='_TAIEX'. Tests cover lock skip,
happy path, idempotent re-run, and discussion-context integration
via the new `tw_market_service.get_index(history_days=...)` shape.
"""
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.ohlcv_daily import OhlcvDaily


def _twse_taiex_row(d: str, close: float = 17500.0) -> dict:
    return {
        "time": d, "open": close, "high": close, "low": close,
        "close": close, "volume": 250_000_000,
    }


@pytest.fixture
def patch_session(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch(
        "tasks.ingest_taiex_history.AsyncSessionLocal",
        return_value=_CM(),
    ):
        yield


@pytest.mark.asyncio
async def test_lock_held_skips_work(patch_session):
    from tasks import ingest_taiex_history

    with patch(
        "tasks.ingest_taiex_history.acquire_lock",
        AsyncMock(return_value=False),
    ), patch(
        "tasks.ingest_taiex_history.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_taiex_history.twse.get_taiex_history", AsyncMock(),
    ) as twse_call:
        await ingest_taiex_history.run()

    twse_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_writes_taiex_bars_with_index_symbol(
    patch_session, db_session: AsyncSession,
):
    from tasks import ingest_taiex_history

    rows = [
        _twse_taiex_row("2026-04-28", close=17500),
        _twse_taiex_row("2026-04-29", close=17600),
        _twse_taiex_row("2026-04-30", close=17400),
    ]

    with patch(
        "tasks.ingest_taiex_history.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.ingest_taiex_history.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_taiex_history.twse.get_taiex_history",
        AsyncMock(return_value=rows),
    ), patch(
        "tasks.ingest_taiex_history.record_health", AsyncMock(),
    ) as health:
        await ingest_taiex_history.run()

    db_rows = (await db_session.scalars(
        select(OhlcvDaily).where(
            OhlcvDaily.symbol == "_TAIEX",
            OhlcvDaily.market == "TW",
        ).order_by(OhlcvDaily.ts.asc())
    )).all()
    assert len(db_rows) == 3
    assert [float(r.close) for r in db_rows] == [17500, 17600, 17400]
    assert all(r.source == "twse" for r in db_rows)

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 3


@pytest.mark.asyncio
async def test_rerun_dedupes_and_updates(
    patch_session, db_session: AsyncSession,
):
    """Same date upserted with updated values overwrites, no dupes."""
    from tasks import ingest_taiex_history

    common = (
        patch(
            "tasks.ingest_taiex_history.acquire_lock",
            AsyncMock(return_value=True),
        ),
        patch("tasks.ingest_taiex_history.release_lock", AsyncMock()),
        patch("tasks.ingest_taiex_history.record_health", AsyncMock()),
    )
    for ctx in common:
        ctx.__enter__()
    try:
        with patch(
            "tasks.ingest_taiex_history.twse.get_taiex_history",
            AsyncMock(return_value=[_twse_taiex_row("2026-04-30", 17400)]),
        ):
            await ingest_taiex_history.run()
        # Same date, corrected value
        with patch(
            "tasks.ingest_taiex_history.twse.get_taiex_history",
            AsyncMock(return_value=[_twse_taiex_row("2026-04-30", 17450)]),
        ):
            await ingest_taiex_history.run()
    finally:
        for ctx in common:
            ctx.__exit__(None, None, None)

    db_rows = (await db_session.scalars(
        select(OhlcvDaily).where(OhlcvDaily.symbol == "_TAIEX")
    )).all()
    assert len(db_rows) == 1
    assert float(db_rows[0].close) == 17450


@pytest.mark.asyncio
async def test_empty_response_records_zero_rows(
    patch_session, db_session: AsyncSession,
):
    from tasks import ingest_taiex_history

    with patch(
        "tasks.ingest_taiex_history.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.ingest_taiex_history.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_taiex_history.twse.get_taiex_history",
        AsyncMock(return_value=[]),
    ), patch(
        "tasks.ingest_taiex_history.record_health", AsyncMock(),
    ) as health:
        await ingest_taiex_history.run()

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 0


@pytest.mark.asyncio
async def test_get_index_with_history_reads_from_db(
    db_session: AsyncSession,
):
    """`get_index(history_days=N)` returns the cached current quote
    plus an N-day history line read from ohlcv_daily under _TAIEX.
    Pre-seed the table directly to verify the read path without
    going through the ingest task."""
    from datetime import date as _date
    from services.ingest.repository import OhlcvBar, upsert_ohlcv_bars
    import services.tw_market_service as tw

    today = _date.today()
    bars = []
    for i in range(5):
        d = today.fromordinal(today.toordinal() - i)
        bars.append(OhlcvBar(
            market="TW", symbol="_TAIEX", ts=d,
            open=17500.0 + i, high=17500.0 + i, low=17500.0 + i,
            close=17500.0 + i, volume=100_000_000, source="twse",
        ))
    await upsert_ohlcv_bars(db_session, bars)

    with patch.object(tw, "cache_get", AsyncMock(return_value=None)), \
         patch.object(tw, "cache_set", AsyncMock()), \
         patch.object(tw.twse, "get_taiex",
                      AsyncMock(return_value={"index": "TAIEX", "value": 17500.0})):
        result = await tw.get_index(history_days=30)

    assert result["index"] == "TAIEX"
    assert "history" in result
    assert len(result["history"]) == 5
    assert all("time" in h and "close" in h for h in result["history"])


@pytest.mark.asyncio
async def test_get_index_default_omits_history():
    """Legacy callers that call `get_index()` (no history_days) keep
    the original single-quote shape — adding the `history` key would
    change downstream serialization unexpectedly."""
    import services.tw_market_service as tw

    with patch.object(tw, "cache_get", AsyncMock(return_value=None)), \
         patch.object(tw, "cache_set", AsyncMock()), \
         patch.object(tw.twse, "get_taiex",
                      AsyncMock(return_value={"index": "TAIEX", "value": 17500.0})):
        result = await tw.get_index()

    assert "history" not in result
