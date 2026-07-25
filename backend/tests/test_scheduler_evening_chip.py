"""The 21:40 Taipei chip re-probe (spec Track 2).

The 17:10 run legitimately finds today's ledgers unpublished
(`not_yet_published`); this second run lands them the same evening so
the 04:00 discussion reads T-1 chips beside T-1 prices instead of T-2.
"""
from unittest.mock import patch


def _registered_jobs():
    """Mirrors the pattern in test_paper_order_matching_task.py:
    `setup_jobs()` is the real registration entry point — patch
    `scheduler.add_job` and inspect the recorded calls."""
    from tasks.scheduler import scheduler, setup_jobs

    with patch.object(scheduler, "add_job") as add_job:
        setup_jobs()
    return {
        call.kwargs["id"]: call for call in add_job.call_args_list if "id" in call.kwargs
    }


def test_evening_chip_jobs_registered_at_1340_utc():
    registered = _registered_jobs()
    for jid in ("ingest_institutional_tw_evening", "ingest_margin_tw_evening"):
        assert jid in registered, f"{jid} not registered"
        trigger = registered[jid].kwargs["trigger"]
        trig = str(trigger)
        assert "hour='13'" in trig and "minute='40'" in trig
        # str(CronTrigger) omits the timezone, so a Taipei-timezone trigger
        # would satisfy the assertion above too — pin the timezone
        # explicitly so a 13:40 Asia/Taipei typo (a different hour in UTC)
        # can't slip through.
        assert str(trigger.timezone) == "UTC"
        assert registered[jid].kwargs["replace_existing"] is True
        assert registered[jid].kwargs["max_instances"] == 1
        assert registered[jid].kwargs["coalesce"] is True

    # Guard the afternoon jobs' own schedule too, so an accidental edit to
    # the existing blocks while adding the evening ones can't slip through.
    afternoon_institutional = str(registered["ingest_institutional_tw"].kwargs["trigger"])
    assert "hour='9'" in afternoon_institutional and "minute='10'" in afternoon_institutional
    afternoon_margin = str(registered["ingest_margin_tw"].kwargs["trigger"])
    assert "hour='7'" in afternoon_margin and "minute='0'" in afternoon_margin


def test_evening_chip_jobs_invoke_the_same_run_functions_as_the_afternoon_jobs():
    registered = _registered_jobs()

    institutional_afternoon = registered["ingest_institutional_tw"].args[0]
    institutional_evening = registered["ingest_institutional_tw_evening"].args[0]
    assert institutional_evening is institutional_afternoon

    margin_afternoon = registered["ingest_margin_tw"].args[0]
    margin_evening = registered["ingest_margin_tw_evening"].args[0]
    assert margin_evening is margin_afternoon
