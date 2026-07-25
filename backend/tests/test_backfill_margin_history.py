"""Tests for scripts.backfill_margin_history.

The script exists because `tw_margin_daily` held eleven days —
MI_MARGN's OpenAPI surface is a today-only snapshot — which left
`margin_latest` null in every replayed session before 2026-07-12.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.tw_chip_metrics import TwMarginDaily
from scripts import backfill_margin_history as bf


@pytest.fixture(autouse=True)
def _patch_sessions(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch.object(bf, "AsyncSessionLocal", return_value=_CM()), \
         patch.object(bf.asyncio, "sleep", AsyncMock()):
        yield


@pytest.fixture(autouse=True)
async def _isolate(db_session: AsyncSession):
    await db_session.execute(delete(TwMarginDaily))
    await db_session.commit()
    yield


def _quotes(balance: int) -> list[dict]:
    return [
        {"symbol": "2330", "name_zh": "台積電", "margin_purchase": 950,
         "margin_balance": balance, "short_sale": 0, "short_balance": 86},
        {"symbol": "1101", "name_zh": "台泥", "margin_purchase": 10,
         "margin_balance": 200, "short_sale": 5, "short_balance": 7},
    ]


@pytest.mark.asyncio
async def test_writes_one_row_per_symbol_per_session(db_session: AsyncSession):
    with patch.object(bf.twse, "get_margin",
                      AsyncMock(return_value=_quotes(28_388))):
        stats = await bf.backfill(
            date(2026, 6, 4), date(2026, 6, 4), dry_run=False, force=False,
        )

    assert stats["written_sessions"] == 1
    rows = (await db_session.scalars(select(TwMarginDaily))).all()
    assert {r.symbol for r in rows} == {"2330", "1101"}
    tsmc = next(r for r in rows if r.symbol == "2330")
    assert tsmc.margin_balance == 28_388
    assert tsmc.short_balance == 86
    assert tsmc.ts == date(2026, 6, 4)


@pytest.mark.asyncio
async def test_sessions_are_requested_by_date_not_in_bulk():
    """Each session needs its own dated request — the whole reason the
    OpenAPI surface can't be used is that it ignores the date."""
    asked: list[date] = []

    async def _get(day):
        asked.append(day)
        return _quotes(1)

    with patch.object(bf.twse, "get_margin", AsyncMock(side_effect=_get)):
        await bf.backfill(
            date(2026, 6, 4), date(2026, 6, 5), dry_run=True, force=False,
        )

    assert asked == [date(2026, 6, 4), date(2026, 6, 5)]


@pytest.mark.asyncio
async def test_weekends_are_never_requested():
    asked: list[date] = []

    async def _get(day):
        asked.append(day)
        return _quotes(1)

    with patch.object(bf.twse, "get_margin", AsyncMock(side_effect=_get)):
        # 06-06 Sat, 06-07 Sun
        await bf.backfill(
            date(2026, 6, 5), date(2026, 6, 8), dry_run=True, force=False,
        )

    assert asked == [date(2026, 6, 5), date(2026, 6, 8)]


@pytest.mark.asyncio
async def test_non_trading_days_are_skipped_without_a_write(
    db_session: AsyncSession,
):
    """A holiday answers with no rows. That is not a failure — it must
    not be counted as one, or a long holiday would look like an outage."""
    with patch.object(bf.twse, "get_margin", AsyncMock(return_value=[])):
        stats = await bf.backfill(
            date(2026, 6, 4), date(2026, 6, 4), dry_run=False, force=False,
        )

    assert stats["non_trading"] == 1
    assert stats["written_sessions"] == 0
    assert stats["failed"] == 0
    assert (await db_session.scalars(select(TwMarginDaily))).all() == []


@pytest.mark.asyncio
async def test_already_archived_sessions_are_skipped(db_session: AsyncSession):
    with patch.object(bf.twse, "get_margin",
                      AsyncMock(return_value=_quotes(1))) as get:
        await bf.backfill(
            date(2026, 6, 4), date(2026, 6, 4), dry_run=False, force=False,
        )
        first_calls = get.await_count
        stats = await bf.backfill(
            date(2026, 6, 4), date(2026, 6, 4), dry_run=False, force=False,
        )

    assert stats["skipped_present"] == 1
    assert get.await_count == first_calls          # no re-fetch


@pytest.mark.asyncio
async def test_force_refetches_an_archived_session(db_session: AsyncSession):
    with patch.object(bf.twse, "get_margin",
                      AsyncMock(return_value=_quotes(111))):
        await bf.backfill(
            date(2026, 6, 4), date(2026, 6, 4), dry_run=False, force=False,
        )
    with patch.object(bf.twse, "get_margin",
                      AsyncMock(return_value=_quotes(222))):
        stats = await bf.backfill(
            date(2026, 6, 4), date(2026, 6, 4), dry_run=False, force=True,
        )

    assert stats["written_sessions"] == 1
    tsmc = (await db_session.scalars(
        select(TwMarginDaily).where(TwMarginDaily.symbol == "2330"),
    )).all()
    assert len(tsmc) == 1
    assert tsmc[0].margin_balance == 222           # overwritten in place


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(db_session: AsyncSession):
    with patch.object(bf.twse, "get_margin",
                      AsyncMock(return_value=_quotes(1))):
        stats = await bf.backfill(
            date(2026, 6, 4), date(2026, 6, 4), dry_run=True, force=False,
        )

    assert stats["written_sessions"] == 1
    assert stats["written_rows"] == 0
    assert (await db_session.scalars(select(TwMarginDaily))).all() == []


@pytest.mark.asyncio
async def test_a_failing_session_does_not_stop_the_walk(
    db_session: AsyncSession,
):
    calls: list[date] = []

    async def _get(day):
        calls.append(day)
        if day == date(2026, 6, 4):
            raise RuntimeError("twse down")
        return _quotes(5)

    with patch.object(bf.twse, "get_margin", AsyncMock(side_effect=_get)):
        stats = await bf.backfill(
            date(2026, 6, 4), date(2026, 6, 5), dry_run=False, force=False,
        )

    assert calls == [date(2026, 6, 4), date(2026, 6, 5)]
    assert stats["failed"] == 1
    assert stats["written_sessions"] == 1


@pytest.mark.asyncio
async def test_main_exits_nonzero_when_every_session_failed():
    """A total outage must be distinguishable from a quiet window —
    a script that always exits 0 is how silent ingest holes persist."""
    with patch.object(bf.twse, "get_margin",
                      AsyncMock(side_effect=RuntimeError("twse down"))):
        code = await bf._main(["--start", "2026-06-04", "--end", "2026-06-04"])
    assert code == 1


@pytest.mark.asyncio
async def test_main_exits_zero_on_a_clean_run():
    with patch.object(bf.twse, "get_margin",
                      AsyncMock(return_value=_quotes(1))):
        code = await bf._main(["--start", "2026-06-04", "--end", "2026-06-04"])
    assert code == 0


@pytest.mark.asyncio
async def test_main_rejects_an_inverted_window():
    code = await bf._main(["--start", "2026-06-05", "--end", "2026-06-04"])
    assert code == 2
