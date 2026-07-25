"""The verify path must INVOKE record_lesson_outcome for abstain rows —
the precise falling-pool check lives inside the service (one gate, one
truth). Experiment rows stay excluded entirely (#264)."""
from types import SimpleNamespace

import pytest

from tasks.verify_discussion_outcome import should_record_lesson_outcome


@pytest.mark.parametrize("verdict,experiment,expected", [
    ("win", False, True),
    ("big_win", False, True),
    ("abstain", False, True),          # NEW: abstain reaches the service
    ("loss", False, False),
    ("unverifiable", False, False),
    ("win", True, False),              # experiment rows never feed the loop
    ("abstain", True, False),
])
def test_should_record_lesson_outcome(verdict, experiment, expected):
    d = SimpleNamespace(
        verdict=verdict,
        candidate_snapshot=(
            {"experiment": "rules_override"} if experiment else {}
        ),
    )
    assert should_record_lesson_outcome(verdict, d) is expected
