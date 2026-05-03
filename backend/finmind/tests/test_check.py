"""Tests for `finmind.scripts.check` — the exit-code-only health checker.

The check script is intentionally allowed to be opinionated about
thresholds (better to wake someone up than miss a stale dataset), so
the tests pin those thresholds rather than the underlying SQL — if a
threshold ever changes intentionally, exactly one test should need
updating to match."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from finmind.models.backfill_progress import BackfillProgress
from finmind.models.dataset_source import DatasetSource
from finmind.scripts.check import run_checks
from finmind.scripts.init_db import seed_dataset_sources


@pytest.mark.asyncio
async def test_check_fails_when_alembic_not_initialized(finmind_session):
    """Empty schema (no `alembic_version_finmind` table) is the most
    common cause of a 'silently broken' deploy — surface it loudly.

    `finmind_session` does create_all but doesn't write the version
    table, so this matches the "fresh PG with no init_db run" case."""
    failures = await run_checks(skip_staleness=True, skip_quota=True)
    # Catalog is also empty (no seed) — both surface as separate failures.
    joined = "\n".join(failures)
    assert "alembic NOT at head" in joined
    assert "catalog mismatch" in joined


@pytest.mark.asyncio
async def test_check_passes_after_init_with_no_enabled_datasets(
    finmind_session, monkeypatch,
):
    """Right after `init_db` the catalog is full but nothing is enabled
    — staleness check has nothing to scan, all green. Simulates a fresh
    deploy where the operator hasn't opted any datasets in yet."""
    # Pretend alembic is at head — the autouse fixtures use
    # create_all, not the migration, so the version table doesn't
    # exist. Patch the alembic check to bypass.
    from finmind.scripts import status as _status

    async def _fake_alembic(session):
        return {"at_head": True, "current": "0002", "expected": "0002"}

    monkeypatch.setattr(_status, "_collect_alembic", _fake_alembic)

    await seed_dataset_sources()

    failures = await run_checks(skip_staleness=True, skip_quota=True)
    assert failures == []


@pytest.mark.asyncio
async def test_check_flags_enabled_dataset_that_never_ran(
    finmind_session, monkeypatch,
):
    """A dataset enabled by the operator but never executed by ingest
    (last_ingest_at IS NULL) is a common deploy footgun — config wired
    up but no scheduler. Treat as a hard failure."""
    from finmind.scripts import status as _status
    monkeypatch.setattr(
        _status,
        "_collect_alembic",
        lambda s: _ok_alembic(),
    )

    await seed_dataset_sources()

    row = await finmind_session.get(DatasetSource, "TaiwanStockPrice")
    row.enabled = True
    row.local_table = "ohlcv_daily"
    await finmind_session.commit()

    failures = await run_checks(skip_quota=True)
    joined = "\n".join(failures)
    assert "TaiwanStockPrice" in joined
    assert "never ran" in joined


@pytest.mark.asyncio
async def test_check_flags_stale_daily_dataset(
    finmind_session, monkeypatch,
):
    """`daily` cadence has a 30-hour budget. An `last_ingest_at` 48
    hours ago is a real outage."""
    from finmind.scripts import status as _status
    monkeypatch.setattr(
        _status,
        "_collect_alembic",
        lambda s: _ok_alembic(),
    )

    await seed_dataset_sources()

    row = await finmind_session.get(DatasetSource, "TaiwanStockPrice")
    row.enabled = True
    row.last_ingest_at = datetime.now(tz=UTC) - timedelta(hours=48)
    await finmind_session.commit()

    failures = await run_checks(skip_quota=True)
    joined = "\n".join(failures)
    assert "TaiwanStockPrice" in joined
    assert "stale" in joined


@pytest.mark.asyncio
async def test_check_tolerates_fresh_daily_dataset(
    finmind_session, monkeypatch,
):
    """An `last_ingest_at` 6 hours ago for a daily dataset is well
    within budget — no failure expected."""
    from finmind.scripts import status as _status
    monkeypatch.setattr(
        _status,
        "_collect_alembic",
        lambda s: _ok_alembic(),
    )

    await seed_dataset_sources()

    row = await finmind_session.get(DatasetSource, "TaiwanStockPrice")
    row.enabled = True
    row.last_ingest_at = datetime.now(tz=UTC) - timedelta(hours=6)
    await finmind_session.commit()

    failures = await run_checks(skip_quota=True)
    assert failures == []


@pytest.mark.asyncio
async def test_check_flags_failed_chunks(finmind_session, monkeypatch):
    from finmind.scripts import status as _status
    monkeypatch.setattr(
        _status,
        "_collect_alembic",
        lambda s: _ok_alembic(),
    )

    await seed_dataset_sources()

    finmind_session.add(
        BackfillProgress(
            dataset_code="TaiwanStockPrice",
            symbol="2330",
            source="finmind",
            range_start=date(2024, 1, 1),
            range_end=date(2024, 1, 31),
            status="failed",
            finished_at=datetime.now(tz=UTC),
        )
    )
    await finmind_session.commit()

    failures = await run_checks(
        max_failed=0, skip_staleness=True, skip_quota=True
    )
    joined = "\n".join(failures)
    assert "1 failed chunks" in joined


@pytest.mark.asyncio
async def test_check_flags_stuck_running_chunks(
    finmind_session, monkeypatch,
):
    """A `running` chunk older than 1 hour is almost certainly an
    orphaned worker — call out separately so the operator can decide
    whether to reset to 'pending'."""
    from finmind.scripts import status as _status
    monkeypatch.setattr(
        _status,
        "_collect_alembic",
        lambda s: _ok_alembic(),
    )

    await seed_dataset_sources()

    finmind_session.add(
        BackfillProgress(
            dataset_code="TaiwanStockPrice",
            symbol="2330",
            source="finmind",
            range_start=date(2024, 1, 1),
            range_end=date(2024, 1, 31),
            status="running",
            started_at=datetime.now(tz=UTC) - timedelta(hours=4),
        )
    )
    await finmind_session.commit()

    failures = await run_checks(skip_staleness=True, skip_quota=True)
    joined = "\n".join(failures)
    assert "stuck in 'running'" in joined


# ── Helpers ────────────────────────────────────────────────────


def _ok_alembic():
    """Returns a fresh awaitable resolving to a healthy alembic dict.
    Used to bypass the alembic version-table check in tests where the
    schema is built via Base.metadata.create_all (which doesn't write
    the version table that the real `_collect_alembic` reads)."""
    async def _coro(session=None):
        return {"at_head": True, "current": "0011", "expected": "0011"}

    return _coro()
