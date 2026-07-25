"""Recount is a from-scratch aggregation, not an increment replay —
idempotent by construction, so running it twice cannot double-count."""
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts.recount_lesson_hits import (
    aggregate_hits,
    describe_range,
    overlap_warning,
    partition_changes,
    would_promote,
)
from scripts.recount_lesson_hits import main as recount_main


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


def test_inlined_is_experiment_matches_the_real_implementation():
    """`scripts.recount_lesson_hits._is_experiment` is a hand-inlined
    copy of `tasks.verify_discussion_outcome.is_experiment` (see that
    function's own docstring for why it isn't imported directly — doing
    so would reopen the engine-building import this file's own
    `test_import_does_not_pull_in_engine_building_task_module` guards
    against). A hand-copy can silently drift from the original as
    either evolves; pin both sides against the same edge shapes so a
    future change to either one that breaks parity fails loudly here
    instead of showing up as a quiet miscount.

    Imports the real module inside the test body, not at module scope
    -- this test file's import-purity test already proves the SCRIPT
    itself is safe to import; this test is just checking behavioral
    parity in-process, same as any other test in this suite, and is
    allowed to pull in whatever it needs to do that.
    """
    from scripts.recount_lesson_hits import _is_experiment
    from tasks.verify_discussion_outcome import is_experiment as real_is_experiment

    class _NoAttrAtAll:
        """A discussion-shaped object with no candidate_snapshot at
        all -- distinct from one that has it set to None."""

    shapes = [
        SimpleNamespace(candidate_snapshot=None),
        SimpleNamespace(candidate_snapshot={}),
        SimpleNamespace(candidate_snapshot=""),
        SimpleNamespace(candidate_snapshot=False),
        SimpleNamespace(candidate_snapshot=0),
        _NoAttrAtAll(),
        SimpleNamespace(candidate_snapshot={"experiment": True}),
        # Not one of the 7 named edge shapes, but load-bearing: every
        # shape above is falsy except the last, and a non-empty dict is
        # ALSO truthy regardless of its keys — so a mutated inline copy
        # that checked `bool(candidate_snapshot)` instead of
        # `.get("experiment")` would pass all 7 named shapes above
        # (verified empirically) while silently misclassifying every
        # non-experiment discussion that happens to carry a non-empty
        # candidate_snapshot dict as an experiment. This shape is the
        # one that actually discriminates that mutation.
        SimpleNamespace(candidate_snapshot={"pool_size": 12}),
    ]
    for shape in shapes:
        assert _is_experiment(shape) == real_is_experiment(shape), shape


# ── main()/_collect() coverage with a fully fake DB session ────────
#
# main() does `from db.session import AsyncSessionLocal` INSIDE its own
# body (deferred, for import-purity), so the patch target is
# `db.session.AsyncSessionLocal` itself -- there is no module-level
# binding on scripts.recount_lesson_hits to patch. Never a real DB:
# `_FakeRecountSession` is a bare stand-in that dispatches canned rows
# by table name (visible in `str(stmt)`) and records `execute()` calls'
# bind params directly, rather than the real in-memory-sqlite db_session
# fixture other tests in this suite use.


class _FakeScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeRecountSession:
    def __init__(self, *, discussions, contexts_sequence, lessons):
        self._discussions = discussions
        self._contexts_queue = list(contexts_sequence)
        self._lessons = lessons
        self.commit = AsyncMock()
        self.executed: list[tuple[int, int]] = []

    async def scalars(self, stmt):
        text = str(stmt)
        if "discussion_round_contexts" in text:
            return _FakeScalarsResult(self._contexts_queue.pop(0))
        if "discussion_lessons" in text:
            return _FakeScalarsResult(self._lessons)
        return _FakeScalarsResult(self._discussions)

    async def execute(self, stmt):
        params = stmt.compile().params
        self.executed.append((params["id_1"], params["hit_count"]))


class _CM:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _discussion(id_, verdict, *, pool_performance=None, candidate_snapshot=None,
                 created_at):
    return SimpleNamespace(
        id=id_, verdict=verdict, pool_performance=pool_performance,
        candidate_snapshot=candidate_snapshot, created_at=created_at,
    )


def _lesson(id_, *, hit_count, usage_count, as_of_date,
            tier="episodic", market="TW"):
    return SimpleNamespace(
        id=id_, hit_count=hit_count, usage_count=usage_count,
        tier=tier, market=market, as_of_date=as_of_date,
    )


def _build_recount_dataset():
    """8 non-experiment discussions + 1 experiment discussion (skipped
    before it ever reaches a context query) walking every branch in
    _collect/main: a win and a falling-pool abstain that qualify, a
    rising-pool abstain that doesn't, an experiment row, and 5 more
    wins that pile onto lesson 50 so it clears the promotion floor.
    6 lessons cover: raised (10, 20, 99), lowered-to-zero (30, 40),
    unchanged-and-promotable (50), a non-episodic tier (40, excluded
    from WOULD-PROMOTE), a second market (40 is "US", excluded from the
    TW/Sunday-cron headline), and an as_of_date older than the evidence
    window (40 again) to trip the overlap WARNING line."""
    discussions = [
        _discussion(1, "win", created_at=datetime(2026, 7, 15, tzinfo=UTC)),
        _discussion(2, "abstain", pool_performance={"avg_return_pct": -1.0},
                    created_at=datetime(2026, 7, 16, tzinfo=UTC)),
        _discussion(3, "loss", candidate_snapshot={"experiment": True},
                    created_at=datetime(2026, 7, 17, tzinfo=UTC)),
        _discussion(4, "abstain", pool_performance={"avg_return_pct": 2.0},
                    created_at=datetime(2026, 7, 18, tzinfo=UTC)),
    ]
    for offset, day in enumerate(range(21, 26)):
        discussions.append(_discussion(
            5 + offset, "win", created_at=datetime(2026, 7, day, tzinfo=UTC),
        ))

    # One entry per non-experiment discussion, in _collect's own walk
    # order (d3 is an experiment -- skipped before its context query).
    contexts_sequence = [
        [{"recent_lessons": {                                  # d1
            "market": [{"id": 10}, {"id": 20}],
            "per_symbol": {"2330": [{"id": 20}, {"id": 99}]},
        }}],
        [{"recent_lessons": {"market": [{"id": 20}]}}],          # d2
        [{"recent_lessons": {"market": [{"id": 30}]}}],          # d4 (doesn't qualify)
        *([[{"recent_lessons": {"market": [{"id": 50}]}}]] * 5),  # d5..d9
    ]

    lessons = [
        _lesson(10, hit_count=0, usage_count=5, as_of_date=date(2026, 5, 10)),
        _lesson(20, hit_count=1, usage_count=2, as_of_date=date(2026, 7, 1)),
        _lesson(30, hit_count=3, usage_count=5, as_of_date=date(2026, 6, 1)),
        _lesson(40, hit_count=2, usage_count=5, tier="semantic", market="US",
                as_of_date=date(2026, 5, 9)),
        _lesson(99, hit_count=0, usage_count=0, as_of_date=date(2026, 7, 20)),
        _lesson(50, hit_count=5, usage_count=8, as_of_date=date(2026, 7, 5)),
    ]
    return discussions, contexts_sequence, lessons


@pytest.mark.asyncio
async def test_main_dry_run_prints_diagnostics_and_writes_nothing(capsys):
    discussions, contexts_sequence, lessons = _build_recount_dataset()
    session = _FakeRecountSession(
        discussions=discussions, contexts_sequence=contexts_sequence,
        lessons=lessons,
    )
    with patch("db.session.AsyncSessionLocal", return_value=_CM(session)):
        await recount_main(apply=False, allow_lower=False)

    out = capsys.readouterr().out
    assert "evidence window (retained discussions seen):" in out
    assert "lesson age range (as_of_date):" in out
    # lesson 40's as_of_date (2026-05-09) predates the earliest
    # discussion (2026-07-15) -- the era-mismatch warning must fire.
    assert "WARNING: evidence window starts" in out
    # 10, 20, 99 raised; 30, 40 lowered (both to zero).
    assert "hit_count raised: 3, lowered: 2 (of which zeroed: 2)" in out
    assert "archive-eligible (usage>0 & hit_count==0):" in out
    # Only lesson 50 (episodic, usage=8, recomputed hits=5) clears the
    # floor; lesson 40 is semantic (excluded) despite being TW-adjacent.
    assert "WOULD-PROMOTE by market: TW=1" in out
    assert "WOULD-PROMOTE (TW, = Sunday cron): 1" in out
    assert "WOULD-PROMOTE (all markets, secondary): 1" in out
    assert "dry-run — pass --apply to write" in out
    assert "applied" not in out

    assert session.executed == []
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_main_apply_without_allow_lower_refuses_the_lowered_rows(capsys):
    discussions, contexts_sequence, lessons = _build_recount_dataset()
    session = _FakeRecountSession(
        discussions=discussions, contexts_sequence=contexts_sequence,
        lessons=lessons,
    )
    with patch("db.session.AsyncSessionLocal", return_value=_CM(session)):
        await recount_main(apply=True, allow_lower=False)

    out = capsys.readouterr().out
    assert "refusing to lower 2 lesson(s)' hit_count" in out
    assert "applied" in out

    # Only the raises (10: 0->1, 20: 1->2, 99: 0->1) get written; the
    # two lowered-to-zero lessons (30, 40) must NOT be executed.
    assert sorted(session.executed) == [(10, 1), (20, 2), (99, 1)]
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_apply_with_allow_lower_writes_every_change(capsys):
    discussions, contexts_sequence, lessons = _build_recount_dataset()
    session = _FakeRecountSession(
        discussions=discussions, contexts_sequence=contexts_sequence,
        lessons=lessons,
    )
    with patch("db.session.AsyncSessionLocal", return_value=_CM(session)):
        await recount_main(apply=True, allow_lower=True)

    out = capsys.readouterr().out
    assert "refusing to lower" not in out
    assert "applied" in out

    assert sorted(session.executed) == [
        (10, 1), (20, 2), (30, 0), (40, 0), (99, 1),
    ]
    session.commit.assert_awaited_once()


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
