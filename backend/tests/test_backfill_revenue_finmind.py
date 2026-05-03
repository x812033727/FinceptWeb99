"""Tests for scripts.backfill_revenue_finmind.

Coverage:
  - Chunking math: 90-day range produces correct (start, end) pairs.
  - Oldest-first walk: chunks fire from start → end so each chunk's
    YoY computation can use rows backfilled by prior chunks.
  - End-to-end YoY computation: backfill 2025-04 + 2026-04 sequentially
    against a fresh DB → Apr 2026 row gets correctly-computed YoY
    against the Apr 2025 row written by the previous chunk.
  - Per-chunk failure isolation: one transient FinMind 500 doesn't
    abort the rest.
  - Overwrites bogus pre-existing rows: a row pre-seeded with
    `revenue_yoy=2026` (the original PR #211 bug signature) gets
    overwritten when the backfill upsert lands.
"""
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.tw_revenue_monthly import TwRevenueMonthly
from scripts import backfill_revenue_finmind as backfill
from services.ingest.repository import (
    RevenueMonthlyRow,
    upsert_revenue_monthly,
)


# ── Chunking ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_walks_oldest_to_newest_in_30_day_chunks():
    """The chunk order matters: walking newest→oldest would prevent
    YoY computation because the prev-year row wouldn't yet be in DB
    when we process the current row. Verify the FinMind calls fire
    in ascending order."""
    calls: list[tuple[str, str]] = []

    async def _fake(start, end_date=None):
        calls.append((start, end_date))
        return []

    with patch.object(
        backfill.finmind, "get_monthly_revenue_market_wide",
        new=AsyncMock(side_effect=_fake),
    ):
        await backfill.backfill(date(2024, 1, 1), date(2024, 3, 31))

    # 30-day chunks: Jan 1-30, Jan 31-Feb 29, Mar 1-30, Mar 31-31
    assert len(calls) == 4
    # Oldest first
    assert calls[0] == ("2024-01-01", "2024-01-30")
    assert calls[1] == ("2024-01-31", "2024-02-29")
    assert calls[-1] == ("2024-03-31", "2024-03-31")


@pytest.mark.asyncio
async def test_backfill_continues_past_chunk_failures():
    """A FinMind 5xx on chunk 2 of 4 must not abort the overall run.
    Total upserted reflects only the chunks that succeeded."""
    call_count = 0

    async def _flaky(start, end_date=None):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("simulated FinMind 500")
        return [{
            "symbol": "2330", "date": start, "revenue": 100_000_000,
        }]

    with patch.object(
        backfill.finmind, "get_monthly_revenue_market_wide",
        new=AsyncMock(side_effect=_flaky),
    ), patch.object(
        backfill, "AsyncSessionLocal", return_value=_NoopCM(),
    ), patch.object(
        backfill, "_enrich_growth_rates",
        new=AsyncMock(side_effect=lambda db, payload: payload),
    ), patch.object(
        backfill, "upsert_revenue_monthly", new=AsyncMock(),
    ):
        fetched, upserted = await backfill.backfill(
            date(2024, 1, 1), date(2024, 3, 31),
        )

    assert call_count == 4
    assert fetched == 3
    assert upserted == 3


# ── End-to-end against the test DB ────────────────────────────────


class _PassthroughCM:
    """Async-context-manager that yields a shared `db_session` so the
    backfill's `AsyncSessionLocal()` writes land in the test's
    transaction."""
    def __init__(self, db_session):
        self._db = db_session

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


class _NoopCM:
    """For tests that mock out the upsert layer entirely."""
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_backfill_computes_yoy_from_previously_backfilled_chunk(
    db_session: AsyncSession,
):
    """End-to-end value test: the backfill walks Apr 2025 → Apr 2026
    sequentially. By the time the chunk containing Apr 2026 processes,
    the Apr 2025 row is already in DB → YoY computes correctly. Without
    the oldest-first ordering this would yield NULL.

    NB: FinMind's `date` field for TaiwanStockMonthRevenue is the
    publish-month-first (YYYY-MM-01), not the publish day. Mocks use
    the same convention so the resulting `ts` matches what production
    rows look like."""
    target_apr_2025 = date(2025, 4, 1)
    target_apr_2026 = date(2026, 4, 1)

    async def _fake(start, end_date=None):
        chunk_start = date.fromisoformat(start)
        chunk_end = date.fromisoformat(end_date)
        out = []
        if chunk_start <= target_apr_2025 <= chunk_end:
            out.append({
                "symbol": "2330", "date": "2025-04-01",
                "revenue": 100_000_000,
            })
        if chunk_start <= target_apr_2026 <= chunk_end:
            out.append({
                "symbol": "2330", "date": "2026-04-01",
                "revenue": 130_000_000,  # +30% YoY vs 2025-04
            })
        return out

    with patch.object(
        backfill.finmind, "get_monthly_revenue_market_wide",
        new=AsyncMock(side_effect=_fake),
    ), patch.object(
        backfill, "AsyncSessionLocal",
        return_value=_PassthroughCM(db_session),
    ):
        await backfill.backfill(date(2025, 4, 1), date(2026, 4, 30))

    row = await db_session.scalar(
        select(TwRevenueMonthly).where(
            TwRevenueMonthly.symbol == "2330",
            TwRevenueMonthly.ts == date(2026, 4, 1),
        )
    )
    assert row is not None
    # (130M - 100M) / 100M * 100 = 30.0
    assert float(row.revenue_yoy) == 30.0


@pytest.mark.asyncio
async def test_backfill_overwrites_bogus_yoy_year_value(
    db_session: AsyncSession,
):
    """The PR #211 bug signature: a row whose `revenue_yoy` was
    accidentally stamped with the year integer (e.g. 2026 for a Feb
    2026 row, since the connector mistook `revenue_year` for a
    growth %). Backfill upsert MUST overwrite this — otherwise the
    bogus value persists indefinitely because the daily cron is
    paywalled and never reaches `_enrich_growth_rates`."""
    # Pre-seed the bogus row that the user reported.
    await upsert_revenue_monthly(db_session, [
        RevenueMonthlyRow(
            market="TW", symbol="1101", ts=date(2026, 2, 1),
            revenue=12_252_892_000,
            revenue_yoy=2026.0,   # ← the bug — year integer leaked
            revenue_mom=1.0,      # ← the bug — month integer leaked
            source="finmind",
        ),
    ])

    target = date(2026, 2, 1)

    async def _fake(start, end_date=None):
        chunk_start = date.fromisoformat(start)
        chunk_end = date.fromisoformat(end_date)
        if chunk_start <= target <= chunk_end:
            return [{
                "symbol": "1101", "date": "2026-02-01",
                "revenue": 12_252_892_000,
            }]
        return []

    with patch.object(
        backfill.finmind, "get_monthly_revenue_market_wide",
        new=AsyncMock(side_effect=_fake),
    ), patch.object(
        backfill, "AsyncSessionLocal",
        return_value=_PassthroughCM(db_session),
    ):
        await backfill.backfill(date(2026, 2, 1), date(2026, 2, 28))

    row = await db_session.scalar(
        select(TwRevenueMonthly).where(
            TwRevenueMonthly.symbol == "1101",
            TwRevenueMonthly.ts == date(2026, 2, 1),
        )
    )
    assert row is not None
    # No prior-year row in DB → yoy must be None (truthful), NOT 2026.
    assert row.revenue_yoy is None
    assert row.revenue_mom is None
