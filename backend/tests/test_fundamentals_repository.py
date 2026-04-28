"""Round-trip tests for the FundamentalsSnapshot repository.

Each test uses a unique symbol (FUN<n>) to prevent cross-test
contamination in the session-scoped DB.
"""
from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.fundamentals_snapshot import FundamentalsSnapshot
from services.ingest.repository import (
    FundamentalsSnapshotRow,
    read_latest_fundamentals,
    upsert_fundamentals_snapshots,
)


def _row(symbol: str, as_of: date, pe: float = 15.0, source: str = "twse") -> FundamentalsSnapshotRow:
    return FundamentalsSnapshotRow(
        market="TW", symbol=symbol, as_of=as_of,
        pe_ratio=pe, pb_ratio=2.5, dividend_yield=4.5,
        eps=None, revenue=None, payload=None, source=source,
    )


@pytest.mark.asyncio
async def test_upsert_inserts_new_rows(db_session: AsyncSession):
    today = date.today()
    rows = [_row("FUN1", today, 15.0), _row("FUN1", today - timedelta(days=1), 14.5)]
    written = await upsert_fundamentals_snapshots(db_session, rows)
    assert written == 2

    saved = (await db_session.scalars(
        select(FundamentalsSnapshot).where(FundamentalsSnapshot.symbol == "FUN1")
    )).all()
    assert {r.as_of for r in saved} == {today, today - timedelta(days=1)}


@pytest.mark.asyncio
async def test_upsert_overwrites_same_day(db_session: AsyncSession):
    """Re-running on the same day overwrites in place (idempotent ingest)."""
    today = date.today()
    await upsert_fundamentals_snapshots(db_session, [_row("FUN2", today, 15.0)])
    await upsert_fundamentals_snapshots(db_session, [_row("FUN2", today, 18.0, source="finmind")])

    row = await db_session.scalar(
        select(FundamentalsSnapshot).where(
            FundamentalsSnapshot.symbol == "FUN2", FundamentalsSnapshot.as_of == today,
        )
    )
    assert row is not None
    assert float(row.pe_ratio) == 18.0
    assert row.source == "finmind"


@pytest.mark.asyncio
async def test_upsert_empty_iterable_is_noop(db_session: AsyncSession):
    written = await upsert_fundamentals_snapshots(db_session, [])
    assert written == 0


@pytest.mark.asyncio
async def test_read_latest_fundamentals_returns_freshest(db_session: AsyncSession):
    today = date.today()
    await upsert_fundamentals_snapshots(db_session, [
        _row("FUN3", today - timedelta(days=2), 15.0),
        _row("FUN3", today, 18.0),
        _row("FUN3", today - timedelta(days=1), 16.0),
    ])

    out = await read_latest_fundamentals(db_session, "TW", "FUN3", max_age_days=7)
    assert out is not None
    assert out["pe_ratio"] == 18.0
    assert out["as_of"] == today.isoformat()


@pytest.mark.asyncio
async def test_read_latest_fundamentals_respects_age_window(db_session: AsyncSession):
    """A snapshot older than max_age_days is treated as missing."""
    old = date.today() - timedelta(days=30)
    await upsert_fundamentals_snapshots(db_session, [_row("FUN4", old, 15.0)])

    out = await read_latest_fundamentals(db_session, "TW", "FUN4", max_age_days=7)
    assert out is None


@pytest.mark.asyncio
async def test_read_latest_fundamentals_empty_when_no_rows(db_session: AsyncSession):
    out = await read_latest_fundamentals(db_session, "TW", "FUN_NONE", max_age_days=7)
    assert out is None


@pytest.mark.asyncio
async def test_read_latest_fundamentals_filters_by_market(db_session: AsyncSession):
    """A US-market snapshot must not satisfy a TW lookup."""
    today = date.today()
    us_row = FundamentalsSnapshotRow(
        market="US", symbol="FUN5", as_of=today,
        pe_ratio=30.0, pb_ratio=5.0, dividend_yield=1.5,
        eps=None, revenue=None, payload=None, source="polygon",
    )
    await upsert_fundamentals_snapshots(db_session, [us_row])

    out = await read_latest_fundamentals(db_session, "TW", "FUN5", max_age_days=7)
    assert out is None
