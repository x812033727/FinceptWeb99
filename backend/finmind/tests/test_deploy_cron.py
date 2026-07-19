"""Static safety checks for the FinceptWeb99 Taiwan cron config."""

from __future__ import annotations

from pathlib import Path

CRON = Path(__file__).resolve().parents[1] / "deploy" / "finmind-cron"


def _job_lines() -> list[str]:
    return [
        line
        for raw in CRON.read_text().splitlines()
        if (line := raw.strip())
        and not line.startswith("#")
        and "=" not in line.split(maxsplit=1)[0]
    ]


def test_tw_cron_is_bound_to_finceptweb99_market_wide_scope():
    jobs = _job_lines()

    assert len(jobs) == 3
    for job in jobs:
        assert " root " in f" {job} "
        assert "cd /opt/finceptweb99" in job
        assert "/usr/local/bin/docker-compose -p finceptweb99" in job
        assert "python -m finmind.scripts.run_due" in job
        assert "--tw-only --skip-per-symbol" in job
        assert "/usr/bin/flock -n" in job
        assert "/opt/finceptweb99/var/finmind99-tw.lock" in job
        assert ">> /var/log/finmind99-tw.log 2>&1" in job


def test_tw_cron_cannot_fan_out_or_target_the_other_stack():
    text = CRON.read_text()

    assert "--universe-from-tw-stock-info" not in text
    assert "--symbols" not in text
    assert "--crypto-universe-from-db" not in text
    assert "cd /opt/finceptweb " not in text
    assert "docker compose" not in text
