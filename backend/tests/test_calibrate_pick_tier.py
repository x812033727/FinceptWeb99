"""Threshold sweep is pure math over graded picks — the operator run
against real data lands in the PR body, not in tests."""
import os
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.calibrate_pick_tier import sweep
from services.daily_pick_tier import T_HALLUC

PICKS = [
    {"consensus": 0.95, "warnings": 0, "excess_pp": 10.0, "win": True},
    {"consensus": 0.90, "warnings": 1, "excess_pp": 5.0,  "win": True},
    {"consensus": 0.80, "warnings": 0, "excess_pp": -3.0, "win": False},
    {"consensus": 0.70, "warnings": 4, "excess_pp": 2.0,  "win": True},
]


def test_sweep_partitions_and_rates():
    rows = sweep(PICKS, thresholds=[0.85])
    row = rows[0]
    assert row["threshold"] == 0.85
    assert row["n_recommend"] == 2            # 0.95 and 0.90 (warnings ok)
    assert row["recommend_win_rate"] == 1.0
    assert row["recommend_mean_excess"] == 7.5
    assert row["n_watch"] == 2
    assert row["watch_win_rate"] == 0.5


def test_sweep_high_threshold_shrinks_tier():
    rows = sweep(PICKS, thresholds=[0.99])
    assert rows[0]["n_recommend"] == 0
    assert rows[0]["recommend_win_rate"] is None


def test_warning_gate_applies_inside_sweep():
    picks = [{"consensus": 0.95, "warnings": T_HALLUC + 1,
              "excess_pp": 8.0, "win": True}]
    rows = sweep(picks, thresholds=[0.85])
    assert rows[0]["n_recommend"] == 0


def test_import_is_pure():
    """The sweep must be importable without building the async engine —
    same in-subprocess check as `test_recount_lesson_hits.py`'s, and for
    the same reason: conftest has already imported `db.session` by the
    time any in-process assertion could run."""
    backend_dir = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env.setdefault("JWT_SECRET_KEY", "pytest-local-secret-key-32chars!!")
    env.setdefault("PUBLIC_REGISTRATION_ENABLED", "true")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import scripts.calibrate_pick_tier\n"
            "assert 'db.session' not in sys.modules, "
            "sorted(m for m in sys.modules if 'db' in m)\n",
        ],
        cwd=str(backend_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_main_sweeps_graded_experiment_picks(db_session, capsys):
    """End-to-end over a seeded experiment band: pick-bearing graded
    rows enter the sweep, abstentions and consensus-less rows are
    skipped, and the selection-rule verdict prints."""
    from models.discussion import Discussion
    from models.ohlcv_daily import OhlcvDaily
    from models.user import User, UserRole
    from scripts import calibrate_pick_tier as mod

    u = User(id=uuid.uuid4(), email="cal@example.com", hashed_password="x",
             role=UserRole.viewer)
    db_session.add(u)
    await db_session.flush()

    def disc(seq, conclusion):
        created = datetime(2026, 7, 20, tzinfo=UTC)
        return Discussion(
            id=uuid.uuid4(), owner_id=u.id, topic="t", rules="r",
            persona_ids=["buffett"], market="TW", status="done",
            current_round=5, auto_run=True, auto_run_sequence=seq,
            as_of_date=date(2026, 7, 10), conclusion=conclusion,
            day1_open_prices={"2330": 100.0},
            day5_close_prices={"2330": 108.0},
            created_at=created, updated_at=created,
        )

    good = {"recommended_symbols": ["2330"], "consensus_score": 0.9,
            "quality_signals": {"hallucination_warnings": []}}
    db_session.add_all([
        disc(900, good),
        disc(901, {"recommended_symbols": []}),                    # abstain
        disc(902, {"recommended_symbols": ["2330"]}),              # no consensus
        disc(1, good),                                             # live band — excluded
    ])
    for offset, close in enumerate([100.5, 101.0, 101.5, 101.8, 102.0]):
        db_session.add(OhlcvDaily(
            market="TW", symbol="_TAIEX_TR", ts=date(2026, 7, 10 + offset),
            open=100.0 if offset == 0 else close,
            high=close, low=close, close=close, volume=0, source="test",
        ))
    await db_session.commit()

    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    import db.session as dbs
    with patch.object(dbs, "AsyncSessionLocal", return_value=_CM()):
        await mod.main()
    out = capsys.readouterr().out
    assert "graded experiment picks: 1 (skipped 1" in out
    assert "selection rule" in out


def test_select_threshold_picks_largest_qualifying_tier():
    from scripts.calibrate_pick_tier import select_threshold

    rows = [
        {"threshold": 0.75, "n_recommend": 9, "recommend_win_rate": 0.85},
        {"threshold": 0.80, "n_recommend": 9, "recommend_win_rate": 0.85},
        {"threshold": 0.90, "n_recommend": 2, "recommend_win_rate": 1.0},
    ]
    # Same coverage at two bars → the lower threshold wins the tie.
    assert select_threshold(rows, 12)["threshold"] == 0.75
    # Nothing qualifies on coverage → None.
    assert select_threshold(rows, 40) is None
