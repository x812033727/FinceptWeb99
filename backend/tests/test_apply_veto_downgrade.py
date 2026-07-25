"""Pure-function tests for veto-downgrade clause apply/revert, plus
main()-path coverage using a fully fake DB session (never a real one —
see module docstring for _CM/_FakeSession, mirroring the precedent
fixture in test_ingest_chip_metrics_tw_task.py's patch_institutional_
session, adapted to a fake session instead of the real in-memory-
sqlite db_session fixture since main() only needs canned rows and a
commit spy, not real ORM persistence).
"""
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts.apply_veto_downgrade import (
    VETO_DOWNGRADE_CLAUSE,
    _default_probe,
    apply_clause,
    archive_stamp,
    resolve_archive_dir,
    revert_clause,
)
from scripts.apply_veto_downgrade import main as veto_main


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


def test_resolve_archive_dir_prefers_env_override():
    # Even a probe that would say "yes, /host-trigger is writable" must
    # lose to an explicit operator override.
    resolved = resolve_archive_dir(
        {"VETO_ARCHIVE_DIR": "/custom/archive"}, probe=lambda p: True,
    )
    assert resolved == Path("/custom/archive")


def test_resolve_archive_dir_falls_back_to_host_trigger_when_writable():
    resolved = resolve_archive_dir(
        {}, probe=lambda p: str(p) == "/host-trigger",
    )
    assert resolved == Path("/host-trigger/rules-archive")


def test_resolve_archive_dir_falls_back_to_docs_when_nothing_else_available():
    resolved = resolve_archive_dir({}, probe=lambda p: False)
    assert resolved == Path("docs/rules-archive")


def test_default_probe_checks_existence_and_writability(tmp_path):
    assert _default_probe(tmp_path) is True
    assert _default_probe(tmp_path / "does-not-exist") is False


# ── main() coverage with a fully fake DB session ──────────────────
#
# main() does `from db.session import AsyncSessionLocal` INSIDE its
# own body (deferred, for import-purity — see the regression test
# below), so the name to patch is `db.session.AsyncSessionLocal`
# itself, not an attribute of scripts.apply_veto_downgrade (no such
# module-level binding exists to patch). Never a real DB: `_FakeSession`
# is a bare stand-in with a scalars() that returns canned SimpleNamespace
# rows and an AsyncMock commit() spy, not the real in-memory-sqlite
# db_session fixture other tests in this suite use.


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, cfgs):
        self._cfgs = cfgs
        self.commit = AsyncMock()

    async def scalars(self, stmt):
        return _FakeResult(self._cfgs)


class _CM:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_main_show_mode_prints_and_mutates_nothing():
    cfg = SimpleNamespace(user_id="u1", rules="原有規則。")
    session = _FakeSession([cfg])
    with patch("db.session.AsyncSessionLocal", return_value=_CM(session)):
        await veto_main("show", force_without_archive=False)

    assert cfg.rules == "原有規則。"
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_main_apply_happy_path_archives_and_commits(tmp_path, monkeypatch):
    monkeypatch.setenv("VETO_ARCHIVE_DIR", str(tmp_path))
    cfg = SimpleNamespace(user_id="u1", rules="原有規則。")
    session = _FakeSession([cfg])
    with patch("db.session.AsyncSessionLocal", return_value=_CM(session)):
        await veto_main("apply", force_without_archive=False)

    assert VETO_DOWNGRADE_CLAUSE in cfg.rules
    session.commit.assert_awaited_once()

    archived = list(tmp_path.glob("rules-u1-*.txt"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == "原有規則。"


@pytest.mark.asyncio
async def test_main_apply_archive_failure_without_force_skips_mutation(
    tmp_path, monkeypatch,
):
    # A FILE (not a directory) sits where the archive dir would need to
    # be created -- mkdir(parents=True) under it raises NotADirectoryError,
    # a realistic stand-in for the container's PermissionError without
    # needing root-only chmod tricks.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("VETO_ARCHIVE_DIR", str(blocker / "rules-archive"))

    cfg = SimpleNamespace(user_id="u1", rules="原有規則。")
    session = _FakeSession([cfg])
    with patch("db.session.AsyncSessionLocal", return_value=_CM(session)):
        await veto_main("apply", force_without_archive=False)

    assert cfg.rules == "原有規則。"          # unchanged
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_main_apply_archive_failure_with_force_proceeds_anyway(
    tmp_path, monkeypatch,
):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("VETO_ARCHIVE_DIR", str(blocker / "rules-archive"))

    cfg = SimpleNamespace(user_id="u1", rules="原有規則。")
    session = _FakeSession([cfg])
    with patch("db.session.AsyncSessionLocal", return_value=_CM(session)):
        await veto_main("apply", force_without_archive=True)

    assert VETO_DOWNGRADE_CLAUSE in cfg.rules
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_revert_restores_archived_text_and_is_a_noop_when_absent(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("VETO_ARCHIVE_DIR", str(tmp_path))
    base = "原有規則。"
    with_clause = SimpleNamespace(user_id="u1", rules=base + VETO_DOWNGRADE_CLAUSE)
    without_clause = SimpleNamespace(user_id="u2", rules=base)
    session = _FakeSession([with_clause, without_clause])
    with patch("db.session.AsyncSessionLocal", return_value=_CM(session)):
        await veto_main("revert", force_without_archive=False)

    assert with_clause.rules == base
    assert without_clause.rules == base   # already absent -> no-op, untouched
    session.commit.assert_awaited_once()  # only u1 was a real mutation

    archived = list(tmp_path.glob("rules-u1-*.txt"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == base + VETO_DOWNGRADE_CLAUSE
    assert not list(tmp_path.glob("rules-u2-*.txt"))   # no-op archives nothing


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
