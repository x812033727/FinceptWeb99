"""Unit tests for scripts.backfill_market_institutional.

The behaviours worth pinning are the ones that made the missing data
invisible in the first place: a range walk that actually covers the
range, a partial failure that doesn't abandon the rest, and a run that
wrote nothing reporting failure instead of a cheerful zero.
"""
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

import scripts.backfill_market_institutional as backfill
from models.tw_holdings_aggregates import TwMarketInstitutionalDaily


class _PassthroughCM:
    def __init__(self, db_session):
        self._db = db_session

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


def _rows(day: str) -> list[dict]:
    """FinMind's per-investor-type row shape for one trading day."""
    return [
        {"date": day, "name": "Foreign_Investor", "buy": 300, "sell": 100},
        {"date": day, "name": "Investment_Trust", "buy": 50, "sell": 20},
        {"date": day, "name": "Dealer_self", "buy": 10, "sell": 30},
    ]


@pytest.mark.asyncio
async def test_backfill_covers_the_whole_range_in_chunks():
    """A range longer than one chunk must produce contiguous,
    non-overlapping calls that span it exactly — the failure this
    guards against is a walk that silently stops early."""
    calls: list[tuple[str, str]] = []

    async def _fake(start, end_date=None):
        calls.append((start, end_date))
        return []

    with patch.object(
        backfill.finmind, "get_total_institutional_market_wide",
        new=AsyncMock(side_effect=_fake),
    ):
        await backfill.backfill(date(2026, 1, 1), date(2026, 5, 1))

    assert calls[0][0] == "2026-01-01"
    assert calls[-1][1] == "2026-05-01"
    # contiguous: each chunk starts the day after the previous ended
    for (_, prev_end), (nxt_start, _) in zip(calls, calls[1:]):
        assert date.fromisoformat(nxt_start) == date.fromisoformat(prev_end) + (
            date(2026, 1, 2) - date(2026, 1, 1)
        )


@pytest.mark.asyncio
async def test_backfill_continues_past_a_chunk_failure():
    """One FinMind 5xx must not abandon the remaining range; the
    returned totals count only what actually landed."""
    call_count = 0

    async def _flaky(start, end_date=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated FinMind 500")
        return _rows(start)

    with patch.object(
        backfill.finmind, "get_total_institutional_market_wide",
        new=AsyncMock(side_effect=_flaky),
    ), patch.object(
        backfill, "AsyncSessionLocal", return_value=_PassthroughCM(None),
    ), patch.object(
        backfill, "upsert_market_institutional_daily",
        new=AsyncMock(return_value=1),
    ):
        fetched, upserted = await backfill.backfill(
            date(2026, 1, 1), date(2026, 5, 1),
        )

    assert call_count > 1
    assert fetched == 3 * (call_count - 1)
    assert upserted == call_count - 1


@pytest.mark.asyncio
async def test_backfill_writes_aggregated_days(db_session: AsyncSession):
    """End-to-end: FinMind's three per-investor rows for a day collapse
    into one `(market, ts)` row with net figures."""
    async def _fake(start, end_date=None):
        if date.fromisoformat(start) <= date(2026, 4, 2) <= date.fromisoformat(end_date):
            return _rows("2026-04-02")
        return []

    with patch.object(
        backfill.finmind, "get_total_institutional_market_wide",
        new=AsyncMock(side_effect=_fake),
    ), patch.object(
        backfill, "AsyncSessionLocal", return_value=_PassthroughCM(db_session),
    ):
        fetched, upserted = await backfill.backfill(
            date(2026, 4, 1), date(2026, 4, 30),
        )

    assert fetched == 3
    assert upserted == 1
    row = await db_session.scalar(
        sa.select(TwMarketInstitutionalDaily).where(
            TwMarketInstitutionalDaily.market == "TW",
            TwMarketInstitutionalDaily.ts == date(2026, 4, 2),
        )
    )
    assert row is not None
    assert int(row.foreign_buy) == 300
    assert int(row.foreign_sell) == 100


def test_main_exits_nonzero_when_nothing_was_written():
    """A run that wrote zero days is a failed backfill, not a quiet
    success — the whole reason this data was missing is that a
    zero-row outcome read as OK.

    Sync on purpose: `main` owns the event loop via `asyncio.run`, so
    it cannot be called from inside an async test.
    """
    with patch.object(
        backfill, "backfill", new=AsyncMock(return_value=(0, 0)),
    ), patch(
        "sys.argv",
        ["backfill_market_institutional", "--start", "2026-04-01",
         "--end", "2026-04-30"],
    ):
        assert backfill.main() == 1
