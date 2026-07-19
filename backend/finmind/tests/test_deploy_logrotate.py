"""Static safety checks for the FinceptWeb99 Taiwan cron log policy."""
from __future__ import annotations

from pathlib import Path

LOGROTATE = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "finmind-tw-logrotate"
)


def _directives() -> set[str]:
    return {
        line.strip()
        for line in LOGROTATE.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith(("/", "}"))
    }


def test_tw_logrotate_targets_only_finceptweb99_log():
    text = LOGROTATE.read_text()

    assert text.count("{") == 1
    assert text.count("}") == 1
    assert "/var/log/finmind99-tw.log {" in text
    assert "/var/log/finmind" not in text.replace(
        "/var/log/finmind99-tw.log", ""
    )


def test_tw_logrotate_has_bounded_safe_policy():
    assert _directives() == {
        "daily",
        "rotate 14",
        "compress",
        "maxsize 50M",
        "copytruncate",
        "missingok",
        "notifempty",
    }
