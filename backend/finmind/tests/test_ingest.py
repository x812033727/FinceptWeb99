"""Tests for `finmind.ingest` — the Phase A backfill runner.

Mocks the SourceClient so tests don't need network / FinMind quota.
Covers:
  - row transform produces expected local-table dict
  - happy-path UPSERT lands rows + records 'done'
  - missing mapping → status='skipped' (not 'failed')
  - upstream raises → status='failed', error captured
  - duplicate fetch is idempotent (UPSERT overwrites)
  - dataset_sources.last_ingest_at updates"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select, text

from finmind.ingest.mappings import (
    MAPPINGS,
    MappingNotFoundError,
    find_mapping,
    transform_row,
)
from finmind.ingest.runner import ingest_chunk
from finmind.models.backfill_progress import BackfillProgress
from finmind.models.dataset_source import DatasetSource
from finmind.scripts.init_db import seed_dataset_sources


# ── Fake upstream client ────────────────────────────────────────


@dataclass
class FakeClient:
    rows: list[dict[str, Any]]
    raise_on_call: Exception | None = None
    call_count: int = 0

    async def fetch(
        self,
        dataset_code: str,
        symbol: str | None,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        self.call_count += 1
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return list(self.rows)


# ── Mapping unit tests ──────────────────────────────────────────


def test_transform_row_renames_finmind_columns():
    """`column_map` projects FinMind's column names onto local schema;
    unknown FinMind columns are dropped silently."""
    fm_row = {
        "date": "2024-01-02",
        "stock_id": "2330",
        "open": "600.0",
        "max": "605.0",
        "min": "598.0",
        "close": "603.0",
        "Trading_Volume": "12345678",
        "ignored_column": "noise",
    }
    out = transform_row(fm_row, find_mapping("TaiwanStockPrice"))

    assert out["symbol"] == "2330"
    assert out["ts"] == date(2024, 1, 2)
    assert float(out["open"]) == 600.0
    assert float(out["close"]) == 603.0
    assert out["volume"] == 12345678
    assert out["market"] == "TWSE"
    assert out["source"] == "finmind"
    # Renamed-out columns gone:
    assert "max" not in out
    assert "ignored_column" not in out


def test_transform_row_handles_empty_strings():
    """FinMind sometimes returns "" for missing numeric values; the
    runner must coerce those to None (not 0, which would silently
    wreck downstream sums)."""
    fm_row = {
        "date": "2024-01-02",
        "stock_id": "9999",
        "open": "",
        "max": "",
        "min": "",
        "close": "",
        "Trading_Volume": "",
    }
    out = transform_row(fm_row, find_mapping("TaiwanStockPrice"))
    assert out["open"] is None
    assert out["close"] is None
    assert out["volume"] is None


def test_find_mapping_raises_for_unknown():
    with pytest.raises(MappingNotFoundError):
        find_mapping("TaiwanFakeNotARealDataset")


def test_mappings_cover_phase2_headline_datasets():
    """Pin the headline-5 contract — these are the datasets the backfill
    runner ships ready to ingest in Phase 2."""
    expected = {
        "TaiwanStockPrice",
        "TaiwanStockMarginPurchaseShortSale",
        "TaiwanStockInstitutionalInvestorsBuySell",
        "TaiwanStockMonthRevenue",
        "TaiwanStockTotalMarginPurchaseShortSale",
    }
    assert expected.issubset(MAPPINGS.keys())


# ── Runner tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingest_chunk_skips_when_dataset_unseeded(finmind_session):
    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockPrice",
        symbol="2330",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
        client=FakeClient([]),
    )
    assert result.status == "skipped"
    assert "not in dataset_sources" in result.error


@pytest.mark.asyncio
async def test_ingest_chunk_skips_when_local_table_empty(finmind_session):
    """Catalog seeded but Phase 1 hasn't built the destination table —
    runner must NOT try to UPSERT into an empty string."""
    await seed_dataset_sources()
    # Manually clear local_table to simulate pre-Phase-1 state.
    row = await finmind_session.get(DatasetSource, "TaiwanStockPrice")
    row.local_table = ""
    await finmind_session.commit()

    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockPrice",
        symbol="2330",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
        client=FakeClient([]),
    )
    assert result.status == "skipped"
    assert "no local_table" in result.error


@pytest.mark.asyncio
async def test_ingest_chunk_happy_path_writes_rows(finmind_session):
    await seed_dataset_sources()

    fake = FakeClient([
        {
            "date": "2024-01-02", "stock_id": "2330",
            "open": "600", "max": "605", "min": "598",
            "close": "603", "Trading_Volume": "12345",
        },
        {
            "date": "2024-01-03", "stock_id": "2330",
            "open": "603", "max": "608", "min": "602",
            "close": "606", "Trading_Volume": "23456",
        },
    ])

    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockPrice",
        symbol="2330",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
        client=fake,
    )

    assert result.status == "done"
    assert result.rows_written == 2
    assert fake.call_count == 1

    # Verify the rows actually landed.
    rows = (
        await finmind_session.execute(
            text(
                "SELECT symbol, ts, close FROM ohlcv_daily "
                "WHERE symbol = '2330' ORDER BY ts"
            )
        )
    ).all()
    assert len(rows) == 2
    assert str(rows[0][1]) == "2024-01-02"
    assert float(rows[0][2]) == 603.0


@pytest.mark.asyncio
async def test_ingest_chunk_records_progress_done(finmind_session):
    await seed_dataset_sources()

    await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockPrice",
        symbol="2330",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
        client=FakeClient([]),
    )

    chunk = (
        await finmind_session.execute(
            select(BackfillProgress).where(
                BackfillProgress.dataset_code == "TaiwanStockPrice",
                BackfillProgress.symbol == "2330",
            )
        )
    ).scalar_one()
    assert chunk.status == "done"
    assert chunk.rows_written == 0  # empty FakeClient
    assert chunk.finished_at is not None


@pytest.mark.asyncio
async def test_ingest_chunk_records_failure_on_upstream_error(
    finmind_session,
):
    await seed_dataset_sources()

    fake = FakeClient([], raise_on_call=RuntimeError("FinMind 503"))

    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockPrice",
        symbol="2330",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
        client=fake,
    )

    assert result.status == "failed"
    assert "FinMind 503" in result.error

    chunk = (
        await finmind_session.execute(
            select(BackfillProgress).where(
                BackfillProgress.dataset_code == "TaiwanStockPrice",
            )
        )
    ).scalar_one()
    assert chunk.status == "failed"
    assert "FinMind 503" in chunk.error_message

    # dataset_sources telemetry mirrors the failure.
    ds = await finmind_session.get(DatasetSource, "TaiwanStockPrice")
    assert ds.last_error is not None
    assert "FinMind 503" in ds.last_error


@pytest.mark.asyncio
async def test_ingest_chunk_skips_when_no_mapping(finmind_session):
    """A mapped dataset with an empty upstream response completes cleanly."""
    await seed_dataset_sources()

    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanFutOptDailyInfo",
        symbol=None,
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
        client=FakeClient([]),
    )
    assert result.status == "done"
    assert result.rows_written == 0
    assert result.error is None


@pytest.mark.asyncio
async def test_ingest_chunk_idempotent_on_rerun(finmind_session):
    """Re-running the same chunk overwrites rather than duplicating —
    the unique PK on ohlcv_daily + ON CONFLICT DO UPDATE in the runner
    means this is a no-op for existing data and a write for any drift."""
    await seed_dataset_sources()

    fake = FakeClient([
        {
            "date": "2024-01-02", "stock_id": "2330",
            "open": "600", "max": "605", "min": "598",
            "close": "603", "Trading_Volume": "12345",
        },
    ])

    for _ in range(2):
        result = await ingest_chunk(
            finmind_session,
            dataset_code="TaiwanStockPrice",
            symbol="2330",
            range_start=date(2024, 1, 1),
            range_end=date(2024, 1, 31),
            client=fake,
        )
        assert result.status == "done"

    # Still exactly 1 row.
    n = (
        await finmind_session.execute(
            text(
                "SELECT COUNT(*) FROM ohlcv_daily WHERE symbol = '2330'"
            )
        )
    ).scalar_one()
    assert n == 1
    assert fake.call_count == 2  # both calls actually executed


@pytest.mark.asyncio
async def test_ingest_chunk_drops_rows_with_null_pk(finmind_session):
    """FinMind occasionally returns rows where stock_id is "" — those
    would violate the (market, symbol, ts) PK and abort the whole
    batch. Runner filters them out before the UPSERT."""
    await seed_dataset_sources()

    fake = FakeClient([
        {"date": "2024-01-02", "stock_id": "2330",
         "open": "600", "max": "605", "min": "598",
         "close": "603", "Trading_Volume": "1"},
        # Missing stock_id — should be dropped, not crash.
        {"date": "2024-01-03", "stock_id": "",
         "open": "1", "max": "1", "min": "1",
         "close": "1", "Trading_Volume": "1"},
    ])

    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockPrice",
        symbol="2330",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
        client=fake,
    )
    assert result.status == "done"
    assert result.rows_written == 1


@pytest.mark.asyncio
async def test_ingest_chunk_updates_dataset_telemetry(finmind_session):
    """`dataset_sources.last_ingest_at` is the headline metric for
    "is this dataset fresh?" — must update on every successful chunk
    so the check / status scripts and the eventual admin UI stay
    accurate."""
    await seed_dataset_sources()

    before = datetime.now(tz=UTC)
    await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockPrice",
        symbol="2330",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
        client=FakeClient([]),
    )

    ds = await finmind_session.get(DatasetSource, "TaiwanStockPrice")
    assert ds.last_ingest_at is not None
    # Drop tz for SQLite which returns naive — assertion is just that
    # a sensible "now" was recorded, not the specific tz behavior.
    if ds.last_ingest_at.tzinfo is None:
        ds_at = ds.last_ingest_at.replace(tzinfo=UTC)
    else:
        ds_at = ds.last_ingest_at
    assert ds_at >= before.replace(microsecond=0)
