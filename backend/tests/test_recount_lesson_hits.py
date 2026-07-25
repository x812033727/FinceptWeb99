"""Recount is a from-scratch aggregation, not an increment replay —
idempotent by construction, so running it twice cannot double-count."""
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from scripts.recount_lesson_hits import (
    aggregate_hits,
    describe_range,
    overlap_warning,
    partition_changes,
    would_promote,
)


def test_aggregate_counts_once_per_qualifying_discussion():
    # (discussion qualifies?, lesson_ids cited)
    rows = [
        (True,  {1, 2}),
        (True,  {2}),
        (False, {1, 2, 3}),
    ]
    assert aggregate_hits(rows) == {1: 1, 2: 2}


def test_would_promote_applies_both_floors():
    # (usage, hits)
    assert would_promote(usage=10, hits=6) is True     # 0.6 exactly
    assert would_promote(usage=10, hits=5) is False    # ratio floor
    assert would_promote(usage=4, hits=4) is False     # usage floor
    assert would_promote(usage=0, hits=0) is False


def test_partition_changes_splits_raised_lowered_zeroed():
    # lesson id -> hit_count: 1 raised, 2 lowered-to-nonzero, 3 zeroed,
    # 4 unchanged, 5 absent from the recount entirely (implicit zero).
    current = {1: 2, 2: 5, 3: 3, 4: 4, 5: 2}
    recomputed = {1: 4, 2: 2, 3: 0, 4: 4}
    raised, lowered, zeroed = partition_changes(current, recomputed)
    assert raised == 1        # lesson 1: 2 -> 4
    assert lowered == 3       # lessons 2, 3, 5
    assert zeroed == 2        # lessons 3 and 5 recount to exactly 0


def test_partition_changes_empty_is_all_zero():
    assert partition_changes({}, {}) == (0, 0, 0)


def test_describe_range_returns_min_max_ignoring_none():
    assert describe_range([date(2026, 5, 9), None, date(2026, 7, 12)]) == (
        date(2026, 5, 9), date(2026, 7, 12),
    )


def test_describe_range_empty_or_all_none_is_none():
    assert describe_range([]) is None
    assert describe_range([None, None]) is None


def test_overlap_warning_fires_when_lessons_predate_evidence_window():
    evidence_range = (
        datetime(2026, 7, 12, tzinfo=UTC),
        datetime(2026, 7, 25, tzinfo=UTC),
    )
    lesson_age_range = (date(2026, 5, 9), date(2026, 7, 20))
    warning = overlap_warning(
        evidence_range=evidence_range, lesson_age_range=lesson_age_range,
    )
    assert warning is not None
    assert "2026-07-12" in warning
    assert "2026-05-09" in warning


def test_overlap_warning_silent_when_windows_fully_overlap():
    evidence_range = (
        datetime(2026, 5, 1, tzinfo=UTC),
        datetime(2026, 7, 25, tzinfo=UTC),
    )
    lesson_age_range = (date(2026, 5, 9), date(2026, 7, 20))
    assert overlap_warning(
        evidence_range=evidence_range, lesson_age_range=lesson_age_range,
    ) is None


def test_overlap_warning_none_when_either_range_missing():
    assert overlap_warning(evidence_range=None, lesson_age_range=None) is None
    assert overlap_warning(
        evidence_range=(datetime(2026, 1, 1, tzinfo=UTC),) * 2,
        lesson_age_range=None,
    ) is None


def test_import_does_not_pull_in_engine_building_task_module():
    """Regression test: this script used to top-level-import
    `is_experiment` from `tasks.verify_discussion_outcome`, which itself
    top-level-imports `db.session` (building the async engine at import
    time) -- so importing this script was never actually import-safe.
    A subprocess is required because by the time any test body in this
    suite runs, `conftest.py` has already imported `db.session` (and
    plenty else) directly -- checking `sys.modules` in-process would
    pass even with the regression reintroduced, since some earlier test
    would have already pulled the offending chain in first. A fresh
    subprocess is the only way to observe *this module's own* import
    graph in isolation.
    """
    backend_dir = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    # Mirror conftest.py's env setup for config.py's eager Settings()
    # validation -- without a strong JWT_SECRET_KEY the bare import
    # fails before we ever get to check sys.modules, which is a false
    # positive for THIS test's concern, not a real failure of it.
    env.setdefault("JWT_SECRET_KEY", "pytest-local-secret-key-32chars!!")
    env.setdefault("PUBLIC_REGISTRATION_ENABLED", "true")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import scripts.recount_lesson_hits\n"
            "assert 'tasks.verify_discussion_outcome' not in sys.modules, "
            "sorted(m for m in sys.modules if 'tasks' in m or 'db.session' in m)\n",
        ],
        cwd=str(backend_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0 and "ValidationError" in result.stderr:
        pytest.skip(
            "environment blocks bare settings import (missing/invalid "
            "config env vars in this sandbox) -- not what this test checks"
        )
    assert result.returncode == 0, result.stderr
