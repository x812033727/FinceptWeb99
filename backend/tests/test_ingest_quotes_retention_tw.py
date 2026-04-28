"""Tests for tasks.ingest_quotes_retention_tw — the daily prune job."""
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def patch_session(db_session):
    """Make AsyncSessionLocal() yield the per-test sqlite session."""
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch("tasks.ingest_quotes_retention_tw.AsyncSessionLocal", return_value=_CM()):
        yield


@pytest.mark.asyncio
async def test_lock_held_skips_work(patch_session):
    from tasks import ingest_quotes_retention_tw

    with patch("tasks.ingest_quotes_retention_tw.acquire_lock",
               AsyncMock(return_value=False)), \
         patch("tasks.ingest_quotes_retention_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_quotes_retention_tw.prune_quote_snapshots",
               AsyncMock()) as prune:
        await ingest_quotes_retention_tw.run()

    prune.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_calls_prune_with_retention_window(patch_session):
    from tasks import ingest_quotes_retention_tw

    with patch("tasks.ingest_quotes_retention_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_quotes_retention_tw.release_lock", AsyncMock()) as release, \
         patch("tasks.ingest_quotes_retention_tw.prune_quote_snapshots",
               AsyncMock(return_value=42)) as prune, \
         patch("tasks.ingest_quotes_retention_tw.record_health",
               AsyncMock()) as health:
        await ingest_quotes_retention_tw.run()

    prune.assert_awaited_once()
    assert prune.await_args.kwargs["older_than_days"] == ingest_quotes_retention_tw.RETENTION_DAYS
    release.assert_awaited_once()

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 42


@pytest.mark.asyncio
async def test_prune_failure_records_failed_health(patch_session):
    from tasks import ingest_quotes_retention_tw

    with patch("tasks.ingest_quotes_retention_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_quotes_retention_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_quotes_retention_tw.prune_quote_snapshots",
               AsyncMock(side_effect=RuntimeError("boom"))), \
         patch("tasks.ingest_quotes_retention_tw.record_health",
               AsyncMock()) as health:
        await ingest_quotes_retention_tw.run()

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["error"] == "boom"
