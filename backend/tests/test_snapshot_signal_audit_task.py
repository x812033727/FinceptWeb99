"""Tests for `tasks.snapshot_signal_audit` — daily signal-audit
snapshot cron.

Covers the four behaviours that determine operator confidence:
  - Lock-held short-circuit skips work entirely (multi-pod safety).
  - Successful run iterates all configured scopes (NULL +
    TW / US / GLOBAL) and records ok=True with a row_count.
  - One scope's failure doesn't block the others — the cron
    catches per-scope exceptions, logs, and continues.
  - The acquired lock is always released, even when the body
    raises, so a transient panic doesn't wedge the next cron tick.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def patch_session(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch(
        "tasks.snapshot_signal_audit.AsyncSessionLocal",
        return_value=_CM(),
    ):
        yield


@pytest.fixture
def no_backoff():
    """Short-circuit Redis-backed backoff helpers so tests reach the
    actual snapshot loop. Mock Redis returns truthy values for ttl
    which would otherwise divert to the backoff-skip branch."""
    with patch(
        "tasks.snapshot_signal_audit.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.snapshot_signal_audit.clear_failures", AsyncMock(),
    ), patch(
        "tasks.snapshot_signal_audit.record_failure",
        AsyncMock(return_value=1),
    ):
        yield


@pytest.mark.asyncio
async def test_lock_held_skips_work(patch_session):
    """Two pods racing the same cron tick — one wins the lock, the
    other must skip without touching the audit snapshot at all."""
    from tasks import snapshot_signal_audit

    snapshot_mock = AsyncMock()
    health_mock = AsyncMock()
    with patch("tasks.snapshot_signal_audit.acquire_lock",
               AsyncMock(return_value=False)), \
         patch("tasks.snapshot_signal_audit.release_lock", AsyncMock()), \
         patch("tasks.snapshot_signal_audit.snapshot_audit_to_history",
               snapshot_mock), \
         patch("tasks.snapshot_signal_audit.record_health", health_mock):
        await snapshot_signal_audit.run()

    snapshot_mock.assert_not_called()
    health_mock.assert_not_called()


@pytest.mark.asyncio
async def test_success_iterates_all_scopes_and_records_health(
    patch_session, no_backoff, db_session: AsyncSession,
):
    """Happy path: lock acquired, snapshot called once per scope
    (NULL + TW + US + GLOBAL = 4 calls), per-scope row_counts roll
    up into the health record's `row_count`, and the lock is
    released."""
    from tasks import snapshot_signal_audit

    # Each scope writes a different number of rows; cron should
    # accumulate 5+3+2+1 = 11.
    snapshot_mock = AsyncMock(side_effect=[5, 3, 2, 1])
    health_mock = AsyncMock()
    release_mock = AsyncMock()
    with patch("tasks.snapshot_signal_audit.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.snapshot_signal_audit.release_lock", release_mock), \
         patch("tasks.snapshot_signal_audit.snapshot_audit_to_history",
               snapshot_mock), \
         patch("tasks.snapshot_signal_audit.record_health", health_mock):
        await snapshot_signal_audit.run()

    # One call per scope. Order matches `_SNAPSHOT_SCOPES`.
    assert snapshot_mock.await_count == 4
    market_args = [
        call.kwargs["market"] for call in snapshot_mock.await_args_list
    ]
    assert market_args == [None, "TW", "US", "GLOBAL"]

    # Health row totals across scopes.
    health_mock.assert_awaited_once()
    health_kwargs = health_mock.await_args.kwargs
    assert health_kwargs["ok"] is True
    assert health_kwargs["row_count"] == 11

    # Lock always released on the success path.
    release_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_one_scope_failure_does_not_block_others(
    patch_session, no_backoff,
):
    """If snapshotting the TW scope raises, the cron must still try
    US + GLOBAL afterwards. Aggregate row_count reflects only the
    successful scopes."""
    from tasks import snapshot_signal_audit

    # NULL succeeds (5 rows) → TW raises → US succeeds (2) → GLOBAL succeeds (1).
    side_effects = [5, RuntimeError("TW upstream down"), 2, 1]
    snapshot_mock = AsyncMock(side_effect=side_effects)
    health_mock = AsyncMock()
    with patch("tasks.snapshot_signal_audit.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.snapshot_signal_audit.release_lock", AsyncMock()), \
         patch("tasks.snapshot_signal_audit.snapshot_audit_to_history",
               snapshot_mock), \
         patch("tasks.snapshot_signal_audit.record_health", health_mock):
        await snapshot_signal_audit.run()

    # All four scopes attempted.
    assert snapshot_mock.await_count == 4
    # Health still reports OK with the partial row_count (5+2+1 = 8).
    health_mock.assert_awaited_once()
    kwargs = health_mock.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 8


@pytest.mark.asyncio
async def test_release_lock_runs_even_when_body_raises(
    patch_session, no_backoff,
):
    """A panic in `record_health` (or anywhere in the try block)
    must not leak the Redis lock — that would wedge the next cron
    tick until the TTL expired."""
    from tasks import snapshot_signal_audit

    release_mock = AsyncMock()
    with patch("tasks.snapshot_signal_audit.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.snapshot_signal_audit.release_lock", release_mock), \
         patch("tasks.snapshot_signal_audit.snapshot_audit_to_history",
               AsyncMock(return_value=0)), \
         patch("tasks.snapshot_signal_audit.record_health",
               AsyncMock(side_effect=RuntimeError("redis down"))):
        # The cron's outer try/except still fires record_health(ok=False)
        # in the except branch, which would re-raise here without the
        # explicit catch — but `tasks.snapshot_signal_audit.run` swallows
        # everything via try/except/finally. The contract under test is
        # simply that release_lock is awaited regardless.
        try:
            await snapshot_signal_audit.run()
        except Exception:
            pass

    release_mock.assert_awaited_once()
