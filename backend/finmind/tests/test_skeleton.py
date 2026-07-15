"""Smoke tests for the FinMind subsystem skeleton.

Confirms:
  - Catalog loads + validates against the FinMind dataset registry
  - ORM models can be inserted/queried against the in-memory schema
  - The seed routine UPSERTs idempotently (re-run is a no-op)
  - The seed preserves runtime state (enabled / last_ingest_at) across
    re-runs — this is the most important invariant for ops, since
    we'll re-run init_db every time Phase 1 lands a new local_table.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from data.tw.finmind_datasets import all_datasets
from finmind.dataset_catalog import CATALOG, all_entries
from finmind.models.backfill_progress import BackfillProgress
from finmind.models.dataset_source import DatasetSource
from finmind.scripts.init_db import seed_dataset_sources


def test_catalog_validates_against_finmind_registry():
    """Every catalog entry must correspond to a known FinMind dataset
    name, and every FinMind dataset must appear in the catalog.
    Validated at import time in `dataset_catalog.py`; this test pins
    the contract."""
    catalog_codes = {entry.dataset_code for _, entry in all_entries()}
    finmind_codes = {spec.name for spec in all_datasets()}
    assert catalog_codes == finmind_codes


def test_catalog_buckets_match_supported_categories():
    """Catalog includes FinMind documents and the supported market extensions."""
    assert set(CATALOG.keys()) == {
        "technical",
        "chip",
        "fundamental",
        "derivative",
        "realtime",
        "convertible_bond",
        "other",
        "macro",
        "crypto",
    }


def test_realtime_entries_are_not_persistable():
    """Realtime snapshots intentionally have no destination table."""
    for entry in CATALOG["realtime"]:
        assert entry.local_table == "", (
            f"realtime dataset {entry.dataset_code} should not be "
            f"persisted (set local_table='')"
        )
        assert entry.ingest_freq == "realtime"


@pytest.mark.asyncio
async def test_seed_inserts_all_catalog_entries(finmind_session):
    seeded, total = await seed_dataset_sources()

    expected_count = len(list(all_entries()))
    assert seeded == expected_count
    assert total == expected_count

    # Sanity: at least one well-known dataset landed correctly.
    row = (
        await finmind_session.execute(
            select(DatasetSource).where(
                DatasetSource.dataset_code == "TaiwanStockPrice"
            )
        )
    ).scalar_one()
    assert row.category == "technical"
    assert row.per_symbol is True
    assert row.primary_source == "finmind"
    assert row.active_source == "finmind"  # seed default
    assert row.enabled is False  # opt-in per dataset


@pytest.mark.asyncio
async def test_seed_is_idempotent(finmind_session):
    await seed_dataset_sources()
    count_after_first = (
        await finmind_session.execute(
            select(func.count()).select_from(DatasetSource)
        )
    ).scalar_one()

    await seed_dataset_sources()
    count_after_second = (
        await finmind_session.execute(
            select(func.count()).select_from(DatasetSource)
        )
    ).scalar_one()

    assert count_after_first == count_after_second


@pytest.mark.asyncio
async def test_seed_preserves_runtime_state(finmind_session):
    """A re-seed (e.g. after Phase 1 fills in a local_table) must NOT
    clobber an operator's enabled flag or the ingest worker's
    last_ingest_at telemetry."""
    await seed_dataset_sources()

    # Operator enables the dataset; ingest worker writes telemetry.
    target = "TaiwanStockMonthRevenue"
    row = (
        await finmind_session.execute(
            select(DatasetSource).where(
                DatasetSource.dataset_code == target
            )
        )
    ).scalar_one()
    row.enabled = True
    row.last_ingest_at = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    row.last_ingest_rows = 1500
    row.last_error = "transient FinMind 502, retried OK"
    await finmind_session.commit()

    await seed_dataset_sources()

    row = (
        await finmind_session.execute(
            select(DatasetSource).where(
                DatasetSource.dataset_code == target
            )
        )
    ).scalar_one()
    assert row.enabled is True
    assert row.last_ingest_at == datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    assert row.last_ingest_rows == 1500
    assert row.last_error == "transient FinMind 502, retried OK"


@pytest.mark.asyncio
async def test_backfill_progress_unique_constraint(finmind_session):
    """Same chunk inserted twice from the same source raises — makes
    accidental duplicate scheduling a hard error, not a silent waste
    of FinMind quota."""
    from datetime import date

    chunk = dict(
        dataset_code="TaiwanStockPrice",
        symbol="2330",
        source="finmind",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
    )
    finmind_session.add(BackfillProgress(**chunk))
    await finmind_session.commit()

    finmind_session.add(BackfillProgress(**chunk))
    with pytest.raises(Exception):  # IntegrityError on Postgres+SQLite both
        await finmind_session.commit()


@pytest.mark.asyncio
async def test_backfill_progress_allows_same_chunk_from_different_sources(
    finmind_session,
):
    """Phase B verification scenario: re-run a Phase A FinMind chunk
    against TWSE to compare. The unique key includes `source`, so this
    must succeed."""
    from datetime import date

    base = dict(
        dataset_code="TaiwanStockPrice",
        symbol="2330",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
    )
    finmind_session.add(BackfillProgress(**base, source="finmind"))
    finmind_session.add(BackfillProgress(**base, source="twse"))
    await finmind_session.commit()

    count = (
        await finmind_session.execute(
            select(func.count()).select_from(BackfillProgress)
        )
    ).scalar_one()
    assert count == 2


@pytest.mark.asyncio
async def test_seed_default_free_plan_inserts_once_then_no_op(finmind_session):
    """`seed_default_free_plan` is called from init_db on every run.
    First call inserts the row; subsequent calls return False without
    overwriting operator customisations."""
    from finmind.billing.quota import (
        _FALLBACK_CALL_LIMIT,
        _FALLBACK_ROW_LIMIT,
        FREE_PLAN_CODE,
    )
    from finmind.models.billing import Plan
    from finmind.scripts.init_db import seed_default_free_plan

    inserted_first = await seed_default_free_plan()
    assert inserted_first is True

    plan = await finmind_session.get(Plan, FREE_PLAN_CODE)
    assert plan is not None
    assert plan.quota_daily_calls == _FALLBACK_CALL_LIMIT
    assert plan.quota_daily_rows == _FALLBACK_ROW_LIMIT
    assert plan.enabled is True

    # Operator bumps the value.
    plan.quota_daily_calls = 555
    await finmind_session.commit()

    inserted_second = await seed_default_free_plan()
    assert inserted_second is False

    plan_after = await finmind_session.get(Plan, FREE_PLAN_CODE)
    assert plan_after.quota_daily_calls == 555  # operator change preserved
