"""Tests for `finmind.scripts.backup` and `finmind.scripts.restore`.

Mocks subprocess.run so tests don't require pg_dump / pg_restore on
the test runner. Coverage focuses on:
  - URL parsing edge cases (asyncpg suffix, missing fields)
  - Filename naming convention (round-trips for restore version check)
  - Safety rails on restore (refuse populated target without --force,
    refuse version mismatch without --force-version)
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from finmind.scripts.backup import (
    default_output_path,
    parse_connection,
    run_pg_dump,
)
from finmind.scripts.restore import (
    is_db_populated,
    parse_alembic_head_from_filename,
)


# ── parse_connection ────────────────────────────────────────────


def test_parse_connection_strips_asyncpg_suffix():
    """SQLAlchemy URL has `+asyncpg`; pg_dump only knows
    `postgresql://`. Wrapper strips."""
    out = parse_connection(
        "postgresql+asyncpg://finmind:secret@db:5433/clone"
    )
    assert out == {
        "user": "finmind",
        "password": "secret",
        "host": "db",
        "port": "5433",
        "database": "clone",
    }


def test_parse_connection_handles_url_encoded_password():
    """Password with special chars (`@`, `/`, `%`) URL-encoded by
    SQLAlchemy — wrapper must un-quote so pg_dump sees the literal."""
    out = parse_connection(
        "postgresql://user:p%40ss%2Fword@host/db"
    )
    assert out["password"] == "p@ss/word"


def test_parse_connection_default_port():
    """Missing port → 5432 (Postgres default)."""
    out = parse_connection("postgresql://u:p@host/db")
    assert out["port"] == "5432"


def test_parse_connection_rejects_empty_url():
    with pytest.raises(ValueError, match="empty"):
        parse_connection("")


def test_parse_connection_rejects_non_postgres_url():
    with pytest.raises(ValueError, match="postgres"):
        parse_connection("sqlite+aiosqlite:///file.db")


def test_parse_connection_rejects_missing_database():
    with pytest.raises(ValueError, match="no database"):
        parse_connection("postgresql://u:p@host")


# ── default_output_path ─────────────────────────────────────────


def test_default_output_path_format(tmp_path, monkeypatch):
    """Filename embeds alembic head so restore can sanity-check
    version match: `finmind_<14-digit ts>_<head>.dump`."""
    monkeypatch.chdir(tmp_path)
    out = default_output_path("0011")
    assert out.parent.name == "backups"
    assert out.parent.exists()  # mkdir(parents=True, exist_ok=True)
    assert out.name.startswith("finmind_")
    assert out.name.endswith("_0011.dump")
    # 14-digit timestamp between the prefix and head.
    parts = out.stem.split("_")
    assert len(parts[1]) == 14
    assert parts[1].isdigit()


# ── run_pg_dump (mocked subprocess) ─────────────────────────────


def test_run_pg_dump_uses_pgpassword_env(tmp_path):
    """Password passes via PGPASSWORD env, NOT argv — keeps it out
    of `ps` output. Verify by checking that argv doesn't carry it."""
    conn = {
        "user": "u", "password": "secret", "host": "h",
        "port": "5432", "database": "d",
    }
    out = tmp_path / "test.dump"

    with patch("finmind.scripts.backup.shutil.which") as mock_which, \
         patch("finmind.scripts.backup.subprocess.run") as mock_run:
        mock_which.return_value = "/usr/bin/pg_dump"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
        )
        run_pg_dump(conn, out)

    args, kwargs = mock_run.call_args
    argv = args[0]
    assert argv[0] == "pg_dump"
    # Password NEVER in argv.
    assert "secret" not in " ".join(argv)
    # But IS in env.
    assert kwargs["env"]["PGPASSWORD"] == "secret"
    # Required portability flags present.
    assert "--no-owner" in argv
    assert "--no-privileges" in argv
    assert "--format" in argv and "custom" in argv


def test_run_pg_dump_raises_when_binary_missing(tmp_path):
    """pg_dump not on PATH → operator-friendly error message."""
    conn = {
        "user": "u", "password": "p", "host": "h",
        "port": "5432", "database": "d",
    }
    with patch("finmind.scripts.backup.shutil.which") as mock_which:
        mock_which.return_value = None
        with pytest.raises(FileNotFoundError, match="postgresql-client"):
            run_pg_dump(conn, tmp_path / "x.dump")


# ── parse_alembic_head_from_filename ────────────────────────────


def test_parse_alembic_head_from_filename_round_trips():
    """backup.py writes `finmind_<ts>_<head>.dump`; restore.py reads
    head back from the same name. End-to-end round-trip test."""
    name = Path("finmind_20260504123456_0011.dump")
    assert parse_alembic_head_from_filename(name) == "0011"


def test_parse_alembic_head_from_filename_handles_unknown_head():
    """`get_alembic_head` returns 'unknown' when version table
    doesn't exist — that string ends up in the filename and round-
    trips back."""
    name = Path("finmind_20260504123456_unknown.dump")
    assert parse_alembic_head_from_filename(name) == "unknown"


def test_parse_alembic_head_from_filename_returns_none_on_garbage():
    """Operator-renamed file → None, restore falls back to
    --force-version flow."""
    assert parse_alembic_head_from_filename(
        Path("my-custom-name.dump")
    ) is None
    assert parse_alembic_head_from_filename(
        Path("finmind_no_timestamp_xx.dump")
    ) is None


# ── is_db_populated ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_db_populated_false_when_table_empty(finmind_session):
    """Fresh schema after create_all but no seed → catalog empty →
    safe to restore over (returns False)."""
    assert await is_db_populated(finmind_session) is False


@pytest.mark.asyncio
async def test_is_db_populated_true_after_seed(finmind_session):
    """After init_db seeds 80 rows → populated → restore refuses
    without --force."""
    from finmind.scripts.init_db import seed_dataset_sources

    await seed_dataset_sources()
    assert await is_db_populated(finmind_session) is True


@pytest.mark.asyncio
async def test_is_db_populated_returns_false_on_missing_table(
    finmind_session, monkeypatch,
):
    """Brand new DB without tables yet (init_db never ran) — the
    SELECT errors out, helper swallows + returns False so restore
    proceeds without hitting an unhelpful "table does not exist"
    pre-flight failure."""
    from sqlalchemy import text
    await finmind_session.execute(text("DROP TABLE dataset_sources"))
    await finmind_session.commit()
    assert await is_db_populated(finmind_session) is False
