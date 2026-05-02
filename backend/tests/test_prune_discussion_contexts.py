"""Tests for tasks.prune_discussion_contexts — daily snapshot GC."""
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

    with patch(
        "tasks.prune_discussion_contexts.AsyncSessionLocal",
        return_value=_CM(),
    ):
        yield


@pytest.mark.asyncio
async def test_lock_held_skips_work(patch_session):
    """Multi-pod safety: another pod holds the lock → no work done."""
    from tasks import prune_discussion_contexts

    with patch(
        "tasks.prune_discussion_contexts.acquire_lock",
        AsyncMock(return_value=False),
    ), patch(
        "tasks.prune_discussion_contexts.release_lock",
        AsyncMock(),
    ), patch(
        "tasks.prune_discussion_contexts.prune_old_round_contexts",
        AsyncMock(),
    ) as prune:
        await prune_discussion_contexts.run()

    prune.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_calls_prune_with_retention_window(patch_session):
    from tasks import prune_discussion_contexts

    with patch(
        "tasks.prune_discussion_contexts.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.prune_discussion_contexts.release_lock",
        AsyncMock(),
    ) as release, patch(
        "tasks.prune_discussion_contexts.prune_old_round_contexts",
        AsyncMock(return_value=17),
    ) as prune, patch(
        "tasks.prune_discussion_contexts.record_health",
        AsyncMock(),
    ) as health:
        await prune_discussion_contexts.run()

    prune.assert_awaited_once()
    assert (
        prune.await_args.kwargs["older_than_days"]
        == prune_discussion_contexts.RETENTION_DAYS
    )
    release.assert_awaited_once()

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 17


@pytest.mark.asyncio
async def test_prune_failure_records_failed_health(patch_session):
    """Connector / DB failure must surface in the health snapshot,
    not crash the cron and leave the job silent."""
    from tasks import prune_discussion_contexts

    with patch(
        "tasks.prune_discussion_contexts.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.prune_discussion_contexts.release_lock",
        AsyncMock(),
    ), patch(
        "tasks.prune_discussion_contexts.prune_old_round_contexts",
        AsyncMock(side_effect=RuntimeError("boom")),
    ), patch(
        "tasks.prune_discussion_contexts.record_health",
        AsyncMock(),
    ) as health:
        await prune_discussion_contexts.run()

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["error"] == "boom"
