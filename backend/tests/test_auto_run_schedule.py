"""Configurable daily auto-run discussion time.

Two tunables (`DISCUSSION_AUTO_RUN_HOUR` / `_MINUTE`, Asia/Taipei) drive
the `auto_run_discussion` schedule; a scheduler-side watcher reschedules
the job when an admin changes them in the runtime-config UI (the
scheduler runs in its own process, so it can't be poked from the API).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import runtime_config_service as rc


def test_auto_run_tunables_registered():
    for key in ("DISCUSSION_AUTO_RUN_HOUR", "DISCUSSION_AUTO_RUN_MINUTE"):
        assert key in rc._REGISTRY
    h = rc._REGISTRY["DISCUSSION_AUTO_RUN_HOUR"]
    assert h.type == "int" and h.min_value == 0 and h.max_value == 23
    m = rc._REGISTRY["DISCUSSION_AUTO_RUN_MINUTE"]
    assert m.type == "int" and m.min_value == 0 and m.max_value == 59


def _session_cm():
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=MagicMock())
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _patches(hour, minute):
    async def _fake_get_int(_db, key):
        return {"DISCUSSION_AUTO_RUN_HOUR": hour,
                "DISCUSSION_AUTO_RUN_MINUTE": minute}[key]
    return (
        patch("db.session.AsyncSessionLocal", return_value=_session_cm()),
        patch("services.runtime_config_service.get_int", side_effect=_fake_get_int),
    )


@pytest.mark.asyncio
async def test_watcher_reschedules_on_change():
    import tasks.scheduler as sch
    sch._auto_run_applied = (4, 0)
    p_db, p_get = _patches(9, 30)
    with p_db, p_get, patch.object(sch.scheduler, "reschedule_job") as resched:
        await sch._reschedule_auto_run_discussion()
    resched.assert_called_once()
    assert resched.call_args.args[0] == "auto_run_discussion"
    trig = str(resched.call_args.kwargs["trigger"])
    assert "hour='9'" in trig and "minute='30'" in trig
    assert sch._auto_run_applied == (9, 30)


@pytest.mark.asyncio
async def test_watcher_noop_when_unchanged():
    import tasks.scheduler as sch
    sch._auto_run_applied = (9, 30)
    p_db, p_get = _patches(9, 30)
    with p_db, p_get, patch.object(sch.scheduler, "reschedule_job") as resched:
        await sch._reschedule_auto_run_discussion()
    resched.assert_not_called()


@pytest.mark.asyncio
async def test_watcher_swallows_missing_job():
    """If this scheduler instance never added auto_run_discussion, a
    JobLookupError from reschedule is swallowed (no crash, no state bump)."""
    from apscheduler.jobstores.base import JobLookupError
    import tasks.scheduler as sch
    sch._auto_run_applied = None
    p_db, p_get = _patches(7, 15)
    with p_db, p_get, patch.object(
        sch.scheduler, "reschedule_job", side_effect=JobLookupError("x"),
    ):
        await sch._reschedule_auto_run_discussion()
    assert sch._auto_run_applied is None  # not marked applied on failure
