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
    read_fundamentals_as_of,
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


# ── read_fundamentals_as_of: the backtest twin ───────────────────


@pytest.mark.asyncio
async def test_read_as_of_picks_the_snapshot_in_force_that_day(
    db_session: AsyncSession,
):
    """`read_latest_fundamentals` anchors on today, so a replay can only
    ever ask "now". This answers "what was public on `as_of`"."""
    anchor = date(2026, 5, 26)
    await upsert_fundamentals_snapshots(db_session, [
        _row("FUN20", anchor - timedelta(days=5), 11.0),
        _row("FUN20", anchor, 13.0),
    ])

    out = await read_fundamentals_as_of(db_session, "TW", "FUN20", as_of=anchor)
    assert out is not None
    assert out["pe_ratio"] == 13.0
    assert out["as_of"] == anchor.isoformat()


@pytest.mark.asyncio
async def test_read_as_of_never_sees_a_later_snapshot(db_session: AsyncSession):
    """The look-ahead boundary. A replay anchored on 05-26 must not read
    the valuation that only existed in July."""
    anchor = date(2026, 5, 26)
    await upsert_fundamentals_snapshots(db_session, [
        _row("FUN21", anchor - timedelta(days=1), 12.0),
        _row("FUN21", anchor + timedelta(days=40), 99.0),   # post-anchor
    ])

    out = await read_fundamentals_as_of(db_session, "TW", "FUN21", as_of=anchor)
    assert out is not None
    assert out["pe_ratio"] == 12.0


@pytest.mark.asyncio
async def test_read_as_of_falls_back_to_the_nearest_earlier_session(
    db_session: AsyncSession,
):
    """No staleness cap: on a day the ingest job never covered, the
    closest earlier snapshot beats showing nothing — and `as_of` on the
    result makes the age visible."""
    anchor = date(2026, 5, 26)
    stale = anchor - timedelta(days=60)
    await upsert_fundamentals_snapshots(db_session, [_row("FUN22", stale, 9.0)])

    out = await read_fundamentals_as_of(db_session, "TW", "FUN22", as_of=anchor)
    assert out is not None
    assert out["pe_ratio"] == 9.0
    assert out["as_of"] == stale.isoformat()


@pytest.mark.asyncio
async def test_read_as_of_empty_when_archive_starts_later(
    db_session: AsyncSession,
):
    anchor = date(2026, 5, 26)
    await upsert_fundamentals_snapshots(db_session, [
        _row("FUN23", anchor + timedelta(days=1), 15.0),
    ])
    assert await read_fundamentals_as_of(
        db_session, "TW", "FUN23", as_of=anchor,
    ) is None
