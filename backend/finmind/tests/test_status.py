"""Tests for `finmind.scripts.status` — the health-report CLI's
data-collection layer.

Renderers (`render_human` / `render_json`) get one smoke-test each;
the rest pin invariants on `collect_status`'s output shape so future
checks built on top of it (`check.py`, eventually `/api/finmind/admin/
status`) can rely on those keys."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from finmind.dataset_catalog import all_entries
from finmind.models.backfill_progress import BackfillProgress
from finmind.models.dataset_source import DatasetSource
from finmind.scripts.init_db import seed_dataset_sources
from finmind.scripts.status import (
    _expected_alembic_head,
    collect_status,
    render_human,
    render_json,
)


def test_expected_alembic_head_comes_from_revision_graph():
    assert _expected_alembic_head() == "0023"


@pytest.mark.asyncio
async def test_status_against_empty_db_reports_uninitialized(finmind_session):
    """No catalog, no migrations, no backfill — every section returns
    its empty-state shape rather than crashing on missing rows."""
    report = await collect_status(finmind_session)

    expected = len(list(all_entries()))
    # Catalog count is 0 (no seed yet) but expected reflects the catalog.
    assert report.catalog["seeded"] == 0
    assert report.catalog["expected"] == expected
    assert report.catalog["ok"] is False

    # No enabled datasets, no backfill chunks, no errors.
    assert report.active_ingestion["enabled"] == 0
    assert report.backfill["pending"] == 0
    assert report.backfill["done"] == 0
    assert report.backfill["stuck_running"] == 0
    assert report.recent_errors == []


@pytest.mark.asyncio
async def test_status_after_seed_shows_full_catalog(finmind_session):
    await seed_dataset_sources()

    report = await collect_status(finmind_session)

    expected = len(list(all_entries()))
    assert report.catalog == {"seeded": expected, "expected": expected, "ok": True}

    # Phase 1 coverage: every category present, never more `built` than
    # `total`. (Absolute counts move as Phase 1 batches land — assert
    # the invariant, not the snapshot.)
    assert set(report.phase1_coverage) == {
        "technical", "chip", "fundamental", "derivative",
        "realtime", "convertible_bond", "other", "macro", "crypto",
    }
    for cat, s in report.phase1_coverage.items():
        assert 0 <= s["built"] <= s["total"]
        assert s["total"] > 0


@pytest.mark.asyncio
async def test_phase1_coverage_counts_built_tables(finmind_session):
    """When a Phase 1 migration fills in `local_table`, the report should
    reflect that — this is the headline progress metric.

    Assert the *delta* rather than the absolute count so this test
    keeps passing as more catalog entries get filled in."""
    await seed_dataset_sources()
    baseline = await collect_status(finmind_session)

    # Pick a dataset with no local_table yet (this scope is invariant
    # even as more Phase 1 batches land).
    target_row = None
    for cat, entries in [(c, list(e)) for c, e in [(c, [r for r in
        (await finmind_session.execute(
            select(DatasetSource).where(
                DatasetSource.category == c,
                DatasetSource.local_table == "",
            ).limit(1)
        )).scalars()]) for c in ["technical", "chip", "fundamental"]]]:
        if entries:
            target_row = entries[0]
            target_cat = cat
            break
    assert target_row is not None, "every category fully built — pick another"

    target_row.local_table = "fake_test_table"
    target_row.enabled = True
    await finmind_session.commit()

    report = await collect_status(finmind_session)

    delta = (
        report.phase1_coverage[target_cat]["built"]
        - baseline.phase1_coverage[target_cat]["built"]
    )
    assert delta == 1
    assert (
        report.active_ingestion["enabled"]
        - baseline.active_ingestion["enabled"]
        == 1
    )


@pytest.mark.asyncio
async def test_status_counts_backfill_by_status(finmind_session):
    """Each status bucket gets its own count, and `stuck_running` is
    reported separately (not double-counted in `running`)."""
    await seed_dataset_sources()

    # Insert one chunk per status, plus one `running` chunk that's
    # been alive too long → should land in stuck_running.
    base = dict(
        dataset_code="TaiwanStockPrice",
        source="finmind",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
    )
    finmind_session.add_all([
        BackfillProgress(**base, symbol="2330", status="done"),
        BackfillProgress(**base, symbol="2317", status="pending"),
        BackfillProgress(**base, symbol="2454", status="failed",
                         finished_at=datetime.now(tz=UTC)),
        BackfillProgress(**base, symbol="2412", status="skipped"),
        BackfillProgress(
            **base,
            symbol="3008",
            status="running",
            started_at=datetime.now(tz=UTC) - timedelta(hours=3),
        ),
    ])
    await finmind_session.commit()

    report = await collect_status(finmind_session)

    bf = report.backfill
    assert bf["done"] == 1
    assert bf["pending"] == 1
    assert bf["failed"] == 1
    assert bf["skipped"] == 1
    assert bf["running"] == 1
    assert bf["stuck_running"] == 1


@pytest.mark.asyncio
async def test_status_surfaces_recent_errors_from_both_sources(
    finmind_session,
):
    """Daily-cron failures (dataset_sources.last_error) AND historical
    chunk failures (backfill_progress) both feed `recent_errors`."""
    await seed_dataset_sources()

    # Daily-cron failure
    ds = await finmind_session.get(DatasetSource, "TaiwanStockMonthRevenue")
    ds_secret = "test-status-dataset-secret"
    ds.last_error = (
        "FinMind 503 for https://example.test/data?"
        f"token={ds_secret}&dataset=Revenue"
    )
    ds.last_ingest_at = datetime(2026, 5, 3, 9, 0, tzinfo=UTC)
    # Backfill chunk failure
    finmind_session.add(
        BackfillProgress(
            dataset_code="TaiwanStockPrice",
            symbol="2330",
            source="finmind",
            range_start=date(2020, 1, 1),
            range_end=date(2020, 1, 31),
            status="failed",
            error_message=(
                "connection reset by peer: Bearer "
                "test-status-backfill-secret"
            ),
            finished_at=datetime.now(tz=UTC) - timedelta(hours=2),
        )
    )
    await finmind_session.commit()

    report = await collect_status(finmind_session)

    sources = {e["source"] for e in report.recent_errors}
    assert sources == {"dataset_sources", "backfill_progress"}

    by_code = {e["dataset_code"]: e for e in report.recent_errors}
    assert "FinMind 503" in by_code["TaiwanStockMonthRevenue"]["error"]
    assert ds_secret not in by_code["TaiwanStockMonthRevenue"]["error"]
    assert "test-status-backfill-secret" not in by_code[
        "TaiwanStockPrice"
    ]["error"]
    assert (
        "connection reset" in by_code["TaiwanStockPrice"]["error"]
    )


@pytest.mark.asyncio
async def test_render_human_includes_key_sections(finmind_session):
    """Smoke test the human renderer — exact formatting is allowed to
    change, but the major section headers must be present so a developer
    grepping the output for "Backfill" or "Quota" never gets surprised."""
    await seed_dataset_sources()

    report = await collect_status(finmind_session)
    out = render_human(report)

    assert "FinMind Clone — Health Report" in out
    assert "Migrations:" in out
    assert "Catalog:" in out
    assert "Phase 1 (schema coverage):" in out
    assert "Active ingestion:" in out
    assert "Backfill progress:" in out
    assert "Recent errors" in out


@pytest.mark.asyncio
async def test_render_json_round_trips(finmind_session):
    """JSON renderer's output must be parseable by callers (e.g. the
    eventual admin UI). datetimes get string-coerced via `default=str`."""
    await seed_dataset_sources()

    report = await collect_status(finmind_session)
    payload = render_json(report)
    parsed = json.loads(payload)

    expected = len(list(all_entries()))
    assert parsed["catalog"]["seeded"] == expected
    assert parsed["catalog"]["expected"] == expected
    assert "generated_at" in parsed


@pytest.mark.asyncio
async def test_status_quota_is_none_when_redis_unavailable(
    finmind_session,
    monkeypatch,
):
    """Redis failures should be swallowed even when CI provides Redis."""
    import redis.asyncio as aioredis

    def _redis_unavailable(*args, **kwargs):
        raise ConnectionError("Redis unavailable")

    monkeypatch.setattr(aioredis, "from_url", _redis_unavailable)

    report = await collect_status(finmind_session)
    assert report.quota is None
