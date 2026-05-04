"""Tests for `FinmindSettings.effective_database_url` + `.schema`.

The new `FINMIND_USE_MAIN_DB=true` mode shares the main app's Postgres
with a `finmind` schema for isolation. These tests pin the resolution
logic (which URL is used + which schema is set) without needing a
live Postgres — pure config-layer unit tests.
"""
from __future__ import annotations

import pytest

from finmind.config import FINMIND_PG_SCHEMA, FinmindSettings


def test_explicit_separate_container_mode_uses_finmind_database_url():
    """Operator opting into Path A1 (FINMIND_USE_MAIN_DB=False) must
    bind to FINMIND_DATABASE_URL — that's the entire point of the
    flag. Default flipped to True in PR #313, so this is now the
    explicit-opt-in path."""
    s = FinmindSettings(
        FINMIND_USE_MAIN_DB=False,
        FINMIND_DATABASE_URL="postgresql+asyncpg://x@h:5433/finmind_clone",
    )
    assert (
        s.effective_database_url
        == "postgresql+asyncpg://x@h:5433/finmind_clone"
    )
    assert s.schema is None  # no isolation needed in a separate DB


def test_default_mode_uses_share_main_db(monkeypatch):
    """New default (PR #313): no env vars set → share main DB via
    `finmind` schema. Eliminates the broken-out-of-the-box experience
    for deploys that don't opt into the `finmind` compose profile."""
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://m@h:5432/finceptweb",
    )
    import config as main_config_module
    import importlib

    importlib.reload(main_config_module)

    s = FinmindSettings()  # NO explicit FINMIND_USE_MAIN_DB
    assert s.FINMIND_USE_MAIN_DB is True
    assert s.schema == "finmind"
    assert s.effective_database_url == (
        "postgresql+asyncpg://m@h:5432/finceptweb"
    )


def test_share_main_db_postgres_uses_finmind_schema(monkeypatch):
    """Postgres + share-mode → main DATABASE_URL with `finmind` schema."""
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://m@h:5432/finceptweb",
    )
    # Force-reload main settings so the patched env var is picked up.
    import config as main_config_module
    import importlib

    importlib.reload(main_config_module)

    s = FinmindSettings(FINMIND_USE_MAIN_DB=True)
    assert s.effective_database_url == (
        "postgresql+asyncpg://m@h:5432/finceptweb"
    )
    assert s.schema == FINMIND_PG_SCHEMA == "finmind"


def test_share_main_db_sqlite_skips_schema(monkeypatch):
    """SQLite (tests / dev) doesn't honour Postgres schemas. When
    share-mode is somehow enabled with a SQLite main DB, schema=None
    so we don't generate `finmind.tablename` references that wouldn't
    work in the test harness."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    import config as main_config_module
    import importlib

    importlib.reload(main_config_module)

    s = FinmindSettings(FINMIND_USE_MAIN_DB=True)
    assert s.effective_database_url == "sqlite+aiosqlite:///:memory:"
    assert s.schema is None


@pytest.mark.parametrize(
    "use_main_db, expected_schema",
    [
        (False, None),  # Path A1 (opt-in): separate container, no schema
        (True, "finmind"),  # Path A2 (default since PR #313): schema=finmind
    ],
)
def test_schema_property_matrix(monkeypatch, use_main_db, expected_schema):
    """Truth table check: schema is None unless we're explicitly sharing
    the main Postgres."""
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://m@h:5432/finceptweb",
    )
    import config as main_config_module
    import importlib

    importlib.reload(main_config_module)

    s = FinmindSettings(FINMIND_USE_MAIN_DB=use_main_db)
    assert s.schema == expected_schema


def test_effective_database_url_safe_masks_password():
    """Diagnostic strings include the URL but never the plaintext
    password — operator needs to see host/port to know which mode is
    active without leaking creds in API response bodies."""
    # FINMIND_USE_MAIN_DB=False to force the FINMIND_DATABASE_URL path
    # (default flipped to True in PR #313).
    s = FinmindSettings(
        FINMIND_USE_MAIN_DB=False,
        FINMIND_DATABASE_URL=(
            "postgresql+asyncpg://finmind:supersecret@db.example.com:5433/finmind_clone"
        ),
    )
    safe = s.effective_database_url_safe
    assert "supersecret" not in safe
    assert "***" in safe
    assert "db.example.com:5433" in safe
    assert safe.startswith("postgresql+asyncpg://finmind:***@")


def test_effective_database_url_safe_masks_password_with_at_sign():
    """A password containing `@` previously broke the regex masker —
    `split('@', 1)` would treat the password's @ as the cred/host
    boundary and leak the password tail in the masked output. The
    SQLAlchemy URL parser handles this correctly."""
    # SQLAlchemy URL spec: passwords with special chars must be
    # URL-encoded. `%40` is encoded `@`; this round-trips cleanly.
    s = FinmindSettings(
        FINMIND_USE_MAIN_DB=False,
        FINMIND_DATABASE_URL=(
            "postgresql+asyncpg://finmind:p%40ss@db.example.com:5433/finmind_clone"
        ),
    )
    safe = s.effective_database_url_safe
    assert "p%40ss" not in safe
    assert "p@ss" not in safe  # decoded form must not leak either
    assert "***" in safe
    assert "db.example.com:5433" in safe


def test_effective_database_url_safe_handles_no_password():
    """Plain `user@host` URLs (no password) round-trip unchanged —
    can't mask what isn't there."""
    s = FinmindSettings(
        FINMIND_USE_MAIN_DB=False,
        FINMIND_DATABASE_URL="postgresql+asyncpg://finmind@h:5433/finmind_clone",
    )
    assert (
        s.effective_database_url_safe
        == "postgresql+asyncpg://finmind@h:5433/finmind_clone"
    )


def test_effective_database_url_safe_handles_sqlite():
    """SQLite URLs have no creds at all — pass through untouched."""
    s = FinmindSettings(
        FINMIND_USE_MAIN_DB=False,
        FINMIND_DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )
    assert (
        s.effective_database_url_safe == "sqlite+aiosqlite:///:memory:"
    )
