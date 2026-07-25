"""Characterization tests freezing the shared `run()` skeleton of the
ingest tasks (lock → backoff gate → _do_run → health/failure bookkeeping
→ unlock).

The ~30 tasks in `tasks/` hand-copy this wiring; R3 extracts it into a
shared `tasks/_runner.py`. These tests pin the CURRENT behavior for
three representative tasks so the extraction is provably behavior-
preserving — they must pass unchanged before AND after the refactor.

All collaborators are patched at the task-module namespace (each task
imports them by name), so no Redis/DB/network is touched.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

import tasks.ingest_institutional_tw as inst_task
import tasks.ingest_margin_tw as margin_task
import tasks.ingest_ohlcv_tw as ohlcv_task

# (module, job_id, _do_run success return). Shapes differ today:
# ohlcv returns (rows, latest_bar_ts); institutional/margin return
# (rows, ok, status) since the chip outcome taxonomy (spec Track 1) —
# the R3 shared runner must preserve all of them.
TASKS = [
    pytest.param(ohlcv_task, "ingest_ohlcv_tw", (42, date(2026, 7, 11)), id="ohlcv"),
    pytest.param(
        inst_task, "ingest_institutional_tw", (42, True, None), id="institutional",
    ),
    pytest.param(margin_task, "ingest_margin_tw", (42, True, None), id="margin"),
]


def _patch_common(mod, **overrides):
    """Patch the shared collaborators on a task module. Returns a dict
    of the mocks keyed by name for assertions."""
    mocks = {
        "acquire_lock": AsyncMock(return_value=True),
        "release_lock": AsyncMock(),
        "backoff_remaining_seconds": AsyncMock(return_value=0),
        "get_failure_count": AsyncMock(return_value=0),
        "get_health": AsyncMock(return_value=None),
        "record_health": AsyncMock(),
        "record_failure": AsyncMock(return_value=1),
        "clear_failures": AsyncMock(),
    }
    mocks.update(overrides)
    patchers = [
        patch.object(mod, name, mock)
        for name, mock in mocks.items()
        if hasattr(mod, name)
    ]
    return mocks, patchers


async def _run_with(mod, mocks, patchers, do_run):
    with patch.object(mod, "_do_run", do_run):
        ctxs = [p.__enter__() for p in patchers]
        try:
            await mod.run()
        finally:
            for p in reversed(patchers):
                p.__exit__(None, None, None)
        return ctxs


@pytest.mark.asyncio
@pytest.mark.parametrize("mod,job_id,success_ret", TASKS)
async def test_lock_held_skips_without_health_record(mod, job_id, success_ret):
    mocks, patchers = _patch_common(mod, acquire_lock=AsyncMock(return_value=False))
    do_run = AsyncMock()
    await _run_with(mod, mocks, patchers, do_run)

    do_run.assert_not_awaited()
    mocks["record_health"].assert_not_awaited()
    # Lock was never acquired → must NOT be released (would clobber the
    # holder's lock).
    mocks["release_lock"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mod,job_id,success_ret", TASKS)
async def test_success_records_ok_health_and_clears_failures(mod, job_id, success_ret):
    mocks, patchers = _patch_common(mod)
    do_run = AsyncMock(return_value=success_ret)
    await _run_with(mod, mocks, patchers, do_run)

    do_run.assert_awaited_once()
    mocks["clear_failures"].assert_awaited_once_with(job_id)
    kwargs = mocks["record_health"].await_args.kwargs
    args = mocks["record_health"].await_args.args
    assert args[0] == job_id
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 42
    mocks["release_lock"].assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mod,job_id,success_ret", TASKS)
async def test_failure_records_failure_and_unhealthy(mod, job_id, success_ret):
    mocks, patchers = _patch_common(mod)
    do_run = AsyncMock(side_effect=RuntimeError("upstream exploded"))
    await _run_with(mod, mocks, patchers, do_run)

    mocks["record_failure"].assert_awaited_once_with(job_id)
    mocks["clear_failures"].assert_not_awaited()
    kwargs = mocks["record_health"].await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["row_count"] == 0
    assert "upstream exploded" in kwargs["error"]
    # Failure count is embedded so operators see the backoff arming.
    assert "failure #1" in kwargs["error"]
    mocks["release_lock"].assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mod,job_id,success_ret", TASKS)
async def test_active_backoff_skips_do_run_but_records_skip(mod, job_id, success_ret):
    mocks, patchers = _patch_common(
        mod,
        backoff_remaining_seconds=AsyncMock(return_value=1800),
        get_failure_count=AsyncMock(return_value=3),
    )
    do_run = AsyncMock()
    await _run_with(mod, mocks, patchers, do_run)

    do_run.assert_not_awaited()
    kwargs = mocks["record_health"].await_args.kwargs
    assert kwargs["ok"] is False
    assert "skipped (backoff after 3 failures" in kwargs["error"]
    mocks["release_lock"].assert_awaited_once()
