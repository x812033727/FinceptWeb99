"""The 22:10/22:30 Taipei index-archive re-runs.

The 15:10/15:30 Taipei slots can run before FinMind has synced the
just-closed session's index rows (observed 2026-07-27: stock OHLCV
landed, `_TAIEX`/`_TAIEX_TR` stayed on 07-24), which left the 04:00
discussion's archive-first index read falling back to live. The
evening re-run (14:10/14:30 UTC) closes that window.
"""
from unittest.mock import patch


def _registered_jobs():
    """Mirrors test_scheduler_evening_chip.py: `setup_jobs()` is the
    real registration entry point — patch `scheduler.add_job` and
    inspect the recorded calls."""
    from tasks.scheduler import scheduler, setup_jobs

    with patch.object(scheduler, "add_job") as add_job:
        setup_jobs()
    return {
        call.kwargs["id"]: call for call in add_job.call_args_list if "id" in call.kwargs
    }


def test_taiex_jobs_run_afternoon_and_evening_utc():
    registered = _registered_jobs()
    for jid, minute in (("ingest_taiex_history", "10"), ("ingest_taiex_tr_history", "30")):
        assert jid in registered, f"{jid} not registered"
        trigger = registered[jid].kwargs["trigger"]
        trig = str(trigger)
        assert "hour='7,14'" in trig, f"{jid}: {trig}"
        assert f"minute='{minute}'" in trig, f"{jid}: {trig}"
        # str(CronTrigger) omits the timezone — pin it so a Taipei-
        # timezone typo (different hours in UTC) can't slip through.
        assert str(trigger.timezone) == "UTC"
        assert registered[jid].kwargs["max_instances"] == 1
        assert registered[jid].kwargs["coalesce"] is True
