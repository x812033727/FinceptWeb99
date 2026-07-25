"""Pure-function tests for veto-downgrade clause apply/revert."""
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.apply_veto_downgrade import (
    VETO_DOWNGRADE_CLAUSE,
    apply_clause,
    archive_stamp,
    revert_clause,
)


def test_apply_appends_once():
    base = "原有規則。"
    once = apply_clause(base)
    assert VETO_DOWNGRADE_CLAUSE in once
    assert apply_clause(once) == once          # idempotent


def test_revert_restores_exactly():
    base = "原有規則。"
    assert revert_clause(apply_clause(base)) == base
    assert revert_clause(base) == base          # no-op when absent


def test_clause_is_strategy_scoped_in_wording():
    assert "量價訊號" in VETO_DOWNGRADE_CLAUSE
    assert "部位上限減半" in VETO_DOWNGRADE_CLAUSE


def test_archive_stamp_formats_utc_datetime():
    """Test the archive timestamp formatting with a fixed datetime.

    This test catches the AttributeError regression that occurred when
    UTC was not properly imported: datetime.now(datetime.UTC) fails
    because UTC is not an attribute of the datetime class. With proper
    imports (from datetime import UTC, datetime), this passes.
    """
    fixed_time = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
    assert archive_stamp(fixed_time) == "20260725T120000Z"


def test_import_does_not_pull_in_engine_building_models():
    """Regression test: importing this script must not build an engine.
    Deferred db.session import into main() means this module's import
    graph should never pull in AsyncSessionLocal at module scope.
    A subprocess is required because by the time any test body in this
    suite runs, conftest.py has already imported db.session and models.
    A fresh subprocess is the only way to observe *this module's own*
    import graph in isolation.
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
            "import scripts.apply_veto_downgrade\n"
            "assert 'db.session' not in sys.modules, "
            "sorted(m for m in sys.modules if 'db' in m or 'models' in m)\n",
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
