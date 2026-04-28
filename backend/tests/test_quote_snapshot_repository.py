"""Round-trip tests for the QuoteSnapshot repository.

Exercises insert (with on-conflict do-nothing), latest-quote lookup
with freshness window, and the retention prune. Uses the same
in-memory SQLite engine as test_ingest_repository.

Each test uses a unique symbol (QSR<n>) to prevent cross-test
contamination in the session-scoped DB — same pattern as
test_alert_service.py.
"""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.quote_snapshot import QuoteSnapshot
from services.ingest.repository import (
    QuoteSnapshotRow,
    insert_quote_snapshot,
    prune_quote_snapshots,
    read_latest_quote,
)


def _row(symbol: str, ts: datetime, price: float = 100.0, source: str = "twse") -> QuoteSnapshotRow:
    return QuoteSnapshotRow(
        market="TW", symbol=symbol, ts=ts,
        last_price=price, change_pct=0.5, prev_close=price - 1, volume=1_000,
        source=source,
    )


@pytest.mark.asyncio
async def test_insert_persists_row(db_session: AsyncSession):
    ts = datetime(2026, 4, 28, 5, 30, tzinfo=UTC)
    await insert_quote_snapshot(db_session, _row("QSR1", ts, 600.0))

    row = await db_session.scalar(select(QuoteSnapshot).where(QuoteSnapshot.symbol == "QSR1"))
    assert row is not None
    assert float(row.last_price) == 600.0
    assert row.source == "twse"


@pytest.mark.asyncio
async def test_insert_duplicate_is_silent_noop(db_session: AsyncSession):
    """Same (market, symbol, ts) inserted twice must not raise."""
    ts = datetime(2026, 4, 28, 5, 30, tzinfo=UTC)
    await insert_quote_snapshot(db_session, _row("QSR2", ts, 600.0))
    await insert_quote_snapshot(db_session, _row("QSR2", ts, 999.0, source="finmind"))

    rows = (await db_session.scalars(
        select(QuoteSnapshot).where(QuoteSnapshot.symbol == "QSR2")
    )).all()
    assert len(rows) == 1
    # First write wins; second insert is a no-op.
    assert float(rows[0].last_price) == 600.0
    assert rows[0].source == "twse"


@pytest.mark.asyncio
async def test_read_latest_quote_returns_freshest(db_session: AsyncSession):
    """With multiple snapshots, the most recent ts wins."""
    now = datetime.now(UTC)
    await insert_quote_snapshot(db_session, _row("QSR3", now - timedelta(minutes=4), 100.0))
    await insert_quote_snapshot(db_session, _row("QSR3", now - timedelta(minutes=2), 102.0))
    await insert_quote_snapshot(db_session, _row("QSR3", now - timedelta(minutes=1), 105.0))

    out = await read_latest_quote(db_session, "TW", "QSR3", max_age_seconds=600)
    assert out is not None
    assert out["price"] == 105.0


@pytest.mark.asyncio
async def test_read_latest_quote_respects_freshness_window(db_session: AsyncSession):
    """A snapshot older than max_age_seconds is treated as missing."""
    now = datetime.now(UTC)
    await insert_quote_snapshot(db_session, _row("QSR4", now - timedelta(minutes=10), 100.0))

    out = await read_latest_quote(db_session, "TW", "QSR4", max_age_seconds=60)
    assert out is None


@pytest.mark.asyncio
async def test_read_latest_quote_returns_none_when_no_rows(db_session: AsyncSession):
    out = await read_latest_quote(db_session, "TW", "QSR_DOES_NOT_EXIST", max_age_seconds=300)
    assert out is None


@pytest.mark.asyncio
async def test_read_latest_quote_filters_by_market(db_session: AsyncSession):
    """A US-market snapshot must not satisfy a TW lookup."""
    now = datetime.now(UTC)
    us_row = QuoteSnapshotRow(
        market="US", symbol="QSR5", ts=now,
        last_price=10, change_pct=0, prev_close=10, volume=1, source="polygon",
    )
    await insert_quote_snapshot(db_session, us_row)

    out = await read_latest_quote(db_session, "TW", "QSR5", max_age_seconds=300)
    assert out is None


@pytest.mark.asyncio
async def test_prune_deletes_older_than_window(db_session: AsyncSession):
    now = datetime.now(UTC)
    await insert_quote_snapshot(db_session, _row("QSR6", now - timedelta(days=45), 100.0))
    await insert_quote_snapshot(db_session, _row("QSR6", now - timedelta(days=15), 102.0))
    await insert_quote_snapshot(db_session, _row("QSR6", now - timedelta(minutes=1), 105.0))

    deleted = await prune_quote_snapshots(db_session, older_than_days=30)
    assert deleted >= 1

    rows = (await db_session.scalars(
        select(QuoteSnapshot).where(QuoteSnapshot.symbol == "QSR6")
    )).all()
    # Only the two recent rows for QSR6 survived; older test data may
    # or may not have been pruned but doesn't affect this symbol's count.
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_prune_zero_when_no_old_rows(db_session: AsyncSession):
    """Symbol with only fresh rows → no rows deleted for it."""
    now = datetime.now(UTC)
    await insert_quote_snapshot(db_session, _row("QSR7", now - timedelta(minutes=1), 105.0))
    deleted = await prune_quote_snapshots(db_session, older_than_days=30)
    # The freshly-inserted QSR7 row must survive even if other test data
    # was pruned away.
    surviving = await db_session.scalar(
        select(QuoteSnapshot).where(QuoteSnapshot.symbol == "QSR7")
    )
    assert surviving is not None
    # `deleted` is non-negative (other tests' old rows may match).
    assert deleted >= 0
