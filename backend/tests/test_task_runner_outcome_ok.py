"""TaskOutcome.ok=False: alarm without backoff.

`gap` (a past session that should have data but doesn't) must surface
as not-ok on the dashboard, but arming the transport backoff would be
wrong — the next scheduled run is exactly what heals it.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from tasks._runner import TaskOutcome, run_ingest_task


def _collabs(**overrides):
    base = dict(
        acquire_lock=AsyncMock(return_value=True),
        release_lock=AsyncMock(),
        backoff_remaining_seconds=AsyncMock(return_value=0),
        get_failure_count=AsyncMock(return_value=0),
        get_health=AsyncMock(return_value=None),
        record_health=AsyncMock(),
        record_failure=AsyncMock(return_value=1),
        clear_failures=AsyncMock(),
        format_error=str,
    )
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_ok_false_records_not_ok_without_backoff():
    c = _collabs()

    async def body():
        return TaskOutcome(row_count=0, status="gap: 2026-07-24", ok=False)

    await run_ingest_task(
        job_id="j", lock_key="k", lock_ttl=60,
        log=logging.getLogger("t"), body=body, **c,
    )
    kwargs = c["record_health"].await_args.kwargs
    assert kwargs["ok"] is False
    assert "gap" in (kwargs.get("error") or "")
    c["record_failure"].assert_not_awaited()


@pytest.mark.asyncio
async def test_default_ok_true_unchanged():
    c = _collabs()

    async def body():
        return TaskOutcome(row_count=5)

    await run_ingest_task(
        job_id="j", lock_key="k", lock_ttl=60,
        log=logging.getLogger("t"), body=body, **c,
    )
    assert c["record_health"].await_args.kwargs["ok"] is True
