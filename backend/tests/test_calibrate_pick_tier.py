"""Threshold sweep is pure math over graded picks — the operator run
against real data lands in the PR body, not in tests."""
import os
import subprocess
import sys
from pathlib import Path

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
