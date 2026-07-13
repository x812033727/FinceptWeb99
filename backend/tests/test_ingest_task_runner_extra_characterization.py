"""Extra characterization coverage for ingest tasks that had no
dedicated test before the R3 shared-runner extraction.

Companion to ``test_ingest_task_runner_characterization.py``. That file
pins the skeleton for three representative tasks; this one adds the four
refactor candidates that previously lacked ANY test — so the extraction
into ``tasks/_runner.py`` is provably behavior-preserving for them too.

Each task is exercised on the three paths the plan calls out:
success, upstream failure (records failure + arms backoff), and
empty-data (recorded as ``ok=True`` with ``row_count=0``). Collaborators
are patched at the task-module namespace (each task exposes them as its
own globals), so no Redis / DB / network is touched.

These assertions hold against BOTH the pre-refactor hand-written
``run()`` and the post-refactor ``run_ingest_task`` delegation.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import tasks.ingest_announcements_tw as ann_tw_task
import tasks.ingest_announcements_us as ann_us_task
import tasks.ingest_news_feeds as news_feeds_task
import tasks.ingest_tw_vix as vix_task

# (module, job_id, success _do_run return, expected success row_count,
#  empty _do_run return, expected empty row_count).
#
# news_feeds / tw_vix return a bare int row-count; the announcements
# tasks return a counters dict whose "attempted" field becomes the
# health row_count — the shared runner must preserve both shapes.
_OK_COUNTERS = {"fetched": 5, "attempted": 3, "err_count": 0}
_EMPTY_COUNTERS = {"fetched": 0, "attempted": 0, "err_count": 0}

TASKS = [
    pytest.param(news_feeds_task, "ingest_news_feeds", 42, 42, 0, 0, id="news_feeds"),
    pytest.param(vix_task, "ingest_tw_vix", 42, 42, 0, 0, id="tw_vix"),
    pytest.param(
        ann_tw_task, "ingest_announcements_tw",
        _OK_COUNTERS, 3, _EMPTY_COUNTERS, 0, id="announcements_tw",
    ),
    pytest.param(
        ann_us_task, "ingest_announcements_us",
        _OK_COUNTERS, 3, _EMPTY_COUNTERS, 0, id="announcements_us",
    ),
]


def _patch_common(mod, **overrides):
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


async def _run_with(mod, patchers, do_run):
    with patch.object(mod, "_do_run", do_run):
        for p in patchers:
            p.__enter__()
        try:
            await mod.run()
        finally:
            for p in reversed(patchers):
                p.__exit__(None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mod,job_id,ok_ret,ok_rows,empty_ret,empty_rows", TASKS,
)
async def test_success_records_ok_and_clears_failures(
    mod, job_id, ok_ret, ok_rows, empty_ret, empty_rows,
):
    mocks, patchers = _patch_common(mod)
    do_run = AsyncMock(return_value=ok_ret)
    await _run_with(mod, patchers, do_run)

    do_run.assert_awaited_once()
    mocks["clear_failures"].assert_awaited_once_with(job_id)
    mocks["record_failure"].assert_not_awaited()
    args = mocks["record_health"].await_args.args
    kwargs = mocks["record_health"].await_args.kwargs
    assert args[0] == job_id
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == ok_rows
    mocks["release_lock"].assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mod,job_id,ok_ret,ok_rows,empty_ret,empty_rows", TASKS,
)
async def test_failure_records_failure_and_unhealthy(
    mod, job_id, ok_ret, ok_rows, empty_ret, empty_rows,
):
    mocks, patchers = _patch_common(mod)
    do_run = AsyncMock(side_effect=RuntimeError("upstream exploded"))
    await _run_with(mod, patchers, do_run)

    mocks["record_failure"].assert_awaited_once_with(job_id)
    mocks["clear_failures"].assert_not_awaited()
    kwargs = mocks["record_health"].await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["row_count"] == 0
    assert "upstream exploded" in kwargs["error"]
    assert "failure #1" in kwargs["error"]
    mocks["release_lock"].assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mod,job_id,ok_ret,ok_rows,empty_ret,empty_rows", TASKS,
)
async def test_empty_data_records_ok_zero_rows(
    mod, job_id, ok_ret, ok_rows, empty_ret, empty_rows,
):
    mocks, patchers = _patch_common(mod)
    do_run = AsyncMock(return_value=empty_ret)
    await _run_with(mod, patchers, do_run)

    do_run.assert_awaited_once()
    mocks["clear_failures"].assert_awaited_once_with(job_id)
    mocks["record_failure"].assert_not_awaited()
    kwargs = mocks["record_health"].await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == empty_rows
    mocks["release_lock"].assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mod,job_id,ok_ret,ok_rows,empty_ret,empty_rows", TASKS,
)
async def test_active_backoff_skips_do_run_but_records_skip(
    mod, job_id, ok_ret, ok_rows, empty_ret, empty_rows,
):
    mocks, patchers = _patch_common(
        mod,
        backoff_remaining_seconds=AsyncMock(return_value=1800),
        get_failure_count=AsyncMock(return_value=3),
    )
    do_run = AsyncMock()
    await _run_with(mod, patchers, do_run)

    do_run.assert_not_awaited()
    kwargs = mocks["record_health"].await_args.kwargs
    assert kwargs["ok"] is False
    assert "skipped (backoff after 3 failures" in kwargs["error"]
    mocks["release_lock"].assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mod,job_id,ok_ret,ok_rows,empty_ret,empty_rows", TASKS,
)
async def test_lock_held_skips_without_health_record(
    mod, job_id, ok_ret, ok_rows, empty_ret, empty_rows,
):
    mocks, patchers = _patch_common(
        mod, acquire_lock=AsyncMock(return_value=False),
    )
    do_run = AsyncMock()
    await _run_with(mod, patchers, do_run)

    do_run.assert_not_awaited()
    mocks["record_health"].assert_not_awaited()
    mocks["release_lock"].assert_not_awaited()
