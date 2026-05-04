"""Tests for `finmind.scripts.deep_backfill` — multi-year resumable
historical ingest.

The CLI itself is thin glue (argparse + a loop); the meaningful logic
is the per-chunk dispatch + resume skip. Tests target:
  - month iterator correctness (boundaries + year crossings)
  - resume skip honors backfill_progress 'done' rows
  - market-wide datasets emit one chunk per month (no symbol fan-out)
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from finmind.models.backfill_progress import BackfillProgress
from finmind.models.dataset_source import DatasetSource
from finmind.scripts.deep_backfill import (
    _already_done,
    _month_end,
    _month_starts,
)
from finmind.scripts.init_db import seed_dataset_sources


# ── _month_starts / _month_end ──────────────────────────────────


def test_month_starts_one_year():
    out = _month_starts(date(2024, 1, 1), date(2024, 12, 31))
    assert len(out) == 12
    assert out[0] == date(2024, 1, 1)
    assert out[-1] == date(2024, 12, 1)


def test_month_starts_handles_partial_first_and_last():
    """`start` mid-month → bucket starts on the 1st of that month."""
    out = _month_starts(date(2024, 3, 15), date(2024, 5, 5))
    assert out == [date(2024, 3, 1), date(2024, 4, 1), date(2024, 5, 1)]


def test_month_starts_crosses_year_boundary():
    out = _month_starts(date(2023, 11, 1), date(2024, 2, 1))
    assert out == [
        date(2023, 11, 1),
        date(2023, 12, 1),
        date(2024, 1, 1),
        date(2024, 2, 1),
    ]


def test_month_end_handles_28_30_31_day_months():
    assert _month_end(date(2024, 1, 1)) == date(2024, 1, 31)
    assert _month_end(date(2024, 2, 1)) == date(2024, 2, 29)  # leap
    assert _month_end(date(2025, 2, 1)) == date(2025, 2, 28)  # non-leap
    assert _month_end(date(2024, 4, 1)) == date(2024, 4, 30)
    assert _month_end(date(2024, 12, 1)) == date(2024, 12, 31)  # year-end


# ── Resume skip ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_already_done_returns_false_when_no_row(finmind_session):
    result = await _already_done(
        finmind_session,
        dataset_code="TaiwanStockPrice",
        symbol="2330",
        source="finmind",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
    )
    assert result is False


@pytest.mark.asyncio
async def test_already_done_returns_true_for_done_chunk(finmind_session):
    """Inserting a 'done' BackfillProgress row → resume skips it."""
    finmind_session.add(
        BackfillProgress(
            dataset_code="TaiwanStockPrice",
            symbol="2330",
            source="finmind",
            range_start=date(2024, 1, 1),
            range_end=date(2024, 1, 31),
            status="done",
            rows_written=100,
            finished_at=datetime.now(tz=timezone.utc),
        )
    )
    await finmind_session.commit()

    result = await _already_done(
        finmind_session,
        dataset_code="TaiwanStockPrice",
        symbol="2330",
        source="finmind",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
    )
    assert result is True


@pytest.mark.asyncio
async def test_already_done_does_not_skip_failed_chunk(finmind_session):
    """A 'failed' chunk should be RE-RUN on the next deep_backfill
    invocation — not silently skipped (which would lose data
    permanently)."""
    finmind_session.add(
        BackfillProgress(
            dataset_code="TaiwanStockPrice",
            symbol="2330",
            source="finmind",
            range_start=date(2024, 1, 1),
            range_end=date(2024, 1, 31),
            status="failed",
            error_message="transient FinMind 503",
            finished_at=datetime.now(tz=timezone.utc),
        )
    )
    await finmind_session.commit()

    result = await _already_done(
        finmind_session,
        dataset_code="TaiwanStockPrice",
        symbol="2330",
        source="finmind",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
    )
    assert result is False


@pytest.mark.asyncio
async def test_already_done_handles_market_wide_symbol_none(finmind_session):
    """Market-wide datasets store NULL symbol — resume must match
    NULL-to-NULL via IS NULL semantics, not column equality (which
    NULL never satisfies)."""
    finmind_session.add(
        BackfillProgress(
            dataset_code="TaiwanStockTotalMarginPurchaseShortSale",
            symbol=None,
            source="finmind",
            range_start=date(2024, 1, 1),
            range_end=date(2024, 1, 31),
            status="done",
            rows_written=22,
            finished_at=datetime.now(tz=timezone.utc),
        )
    )
    await finmind_session.commit()

    result = await _already_done(
        finmind_session,
        dataset_code="TaiwanStockTotalMarginPurchaseShortSale",
        symbol=None,
        source="finmind",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
    )
    assert result is True


@pytest.mark.asyncio
async def test_seed_dataset_sources_visible_to_resolver(finmind_session):
    """Smoke: after init_db seeding, the resolver paths in
    deep_backfill can read DatasetSource rows. Pins that future
    refactors don't break the resolver's session contract."""
    await seed_dataset_sources()
    ds = await finmind_session.get(DatasetSource, "TaiwanStockPrice")
    assert ds is not None
    assert ds.per_symbol is True
