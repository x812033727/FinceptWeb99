"""An ingest that asked upstream for data and got nothing is not healthy.

`record_health(ok=True, row_count=0)` is how a broken fetch hid for
months: `tw_revenue_monthly` sat empty while its job reported ok every
single day, and four other tables froze at `today - lookback` the same
way. The dashboard read 29/31 green throughout.

Zero rows is only healthy when there was nothing to ask for (a holiday,
a window already archived) — which the task knows and the runner does
not. So the task says so, and saying nothing keeps today's behaviour.
"""
from unittest.mock import AsyncMock, patch

import pytest

from tasks._runner import TaskOutcome, run_ingest_task


def _collaborators(**overrides):
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
    return mocks


async def _run(outcome: TaskOutcome, **overrides):
    mocks = _collaborators(**overrides)
    import logging

    await run_ingest_task(
        job_id="test_job",
        lock_key="lock:test_job",
        lock_ttl=60,
        log=logging.getLogger("test"),
        body=AsyncMock(return_value=outcome),
        format_error=lambda exc: str(exc),
        **mocks,
    )
    return mocks


@pytest.mark.asyncio
async def test_empty_result_is_flagged_when_the_task_expected_rows():
    mocks = await _run(TaskOutcome(row_count=0, empty_is_stale=True))

    kwargs = mocks["record_health"].await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["row_count"] == 0
    assert "no rows" in (kwargs.get("error") or "").lower()


@pytest.mark.asyncio
async def test_flagging_empty_does_not_arm_the_backoff():
    """The upstream answered — it just had nothing. Arming the
    exponential backoff would stop us asking again tomorrow, which is
    the opposite of what a stale table needs."""
    mocks = await _run(TaskOutcome(row_count=0, empty_is_stale=True))

    mocks["record_failure"].assert_not_awaited()
    mocks["clear_failures"].assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_is_healthy_when_the_task_had_nothing_to_ask_for():
    """A holiday, or a window already fully archived."""
    mocks = await _run(TaskOutcome(row_count=0, empty_is_stale=False))

    assert mocks["record_health"].await_args.kwargs["ok"] is True


@pytest.mark.asyncio
async def test_default_keeps_todays_behaviour():
    """~30 tasks construct TaskOutcome without this flag; none of them
    should change meaning until they opt in."""
    mocks = await _run(TaskOutcome(row_count=0))

    assert mocks["record_health"].await_args.kwargs["ok"] is True


@pytest.mark.asyncio
async def test_rows_written_stays_healthy_even_when_flagged():
    mocks = await _run(TaskOutcome(row_count=5, empty_is_stale=True))

    kwargs = mocks["record_health"].await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 5


@pytest.mark.asyncio
async def test_revenue_job_flags_an_all_empty_window():
    """The job asks for every month-first in a 90-day window and each of
    those months carries ~2,300 companies. Zero rows across all of them
    is a broken fetch, not a quiet quarter — this is precisely how an
    empty `tw_revenue_monthly` read green for months."""
    from tasks import ingest_revenue_tw

    health = AsyncMock()
    with patch("tasks.ingest_revenue_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_revenue_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_revenue_tw.backoff_remaining_seconds", AsyncMock(return_value=0)), \
         patch("tasks.ingest_revenue_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_revenue_tw.record_failure", AsyncMock()) as record_failure, \
         patch("tasks.ingest_revenue_tw.record_health", health), \
         patch("tasks.ingest_revenue_tw.finmind.get_monthly_revenue_market_wide",
               AsyncMock(return_value=[])):
        await ingest_revenue_tw.run()

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["row_count"] == 0
    assert "no rows" in kwargs["error"].lower()
    # The upstream answered — arming the backoff would just delay
    # tomorrow's attempt.
    record_failure.assert_not_awaited()
