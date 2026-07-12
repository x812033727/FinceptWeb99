"""Pytest fixtures for the FinMind subsystem.

Independent of `backend/tests/conftest.py` — runs against an in-memory
SQLite DB built from `finmind.db.base.Base.metadata` only, so the main
app's schema isn't required here. This means:

  - `pytest backend/finmind/tests/` works in any environment that can
    install `aiosqlite + sqlalchemy + pytest-asyncio`
  - tests in this tree never touch the real `finmind_clone` Postgres
    nor the main `finceptweb` Postgres

Pattern mirrors `backend/tests/conftest.py`'s autouse session override
but scoped to the `FinmindAsyncSessionLocal` factory only.
"""
from __future__ import annotations

import os

# Defensive: the FinMind subsystem doesn't read JWT_SECRET_KEY itself,
# but importing other backend modules transitively might. Set a strong
# placeholder so any cross-module test import doesn't trip the
# Settings validator.
os.environ.setdefault("JWT_SECRET_KEY", "pytest-finmind-secret-key-32chars!!")

# Force FinMind subsystem onto an in-memory SQLite URL BEFORE
# `finmind.config` / `finmind.db.session` are imported. The session
# module instantiates the async engine eagerly at import time, so the
# default Postgres URL would force asyncpg to be importable even in
# pure-unit-test environments. This override matches the autouse
# monkeypatch below — both layers are needed because the env var
# controls module-import-time engine creation, while the monkeypatch
# controls runtime SessionLocal calls.
os.environ["FINMIND_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Single import — `finmind/models/__init__.py` re-exports every model
# module so adding a new model file registers it on Base.metadata
# without conftest churn.
import finmind.models  # noqa: E402,F401
from finmind.db.base import Base  # noqa: E402


@pytest_asyncio.fixture
async def finmind_engine():
    """Fresh in-memory SQLite per test — no cross-test state leakage."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def finmind_session(finmind_engine) -> AsyncSession:
    SessionLocal = async_sessionmaker(
        bind=finmind_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with SessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
def _override_finmind_session_factory(finmind_engine, monkeypatch):
    """Redirect every `FinmindAsyncSessionLocal()` call in production
    code to the in-memory engine for the duration of one test.

    Runs after the `finmind_engine` fixture so the bound engine is the
    same one tests query against. Mirrors the
    `_override_async_session_local` autouse fixture in the main test
    suite (CLAUDE.md "two autouse fixtures").
    """
    SessionLocal = async_sessionmaker(
        bind=finmind_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    import finmind.db.session as _fm_session

    monkeypatch.setattr(_fm_session, "FinmindAsyncSessionLocal", SessionLocal)
    monkeypatch.setattr(_fm_session, "finmind_engine", finmind_engine)

    # Also patch the symbol re-exported at call sites the script uses.
    # Each script imports `FinmindAsyncSessionLocal` at module load, so
    # changing it on `finmind.db.session` doesn't propagate to those
    # scripts — patch each one explicitly.
    import finmind.scripts.check as _check
    import finmind.scripts.cutover as _cutover
    import finmind.scripts.diagnostic as _diagnostic
    import finmind.scripts.init_db as _init_db
    import finmind.scripts.status as _status

    for mod in (_init_db, _status, _check, _diagnostic, _cutover):
        monkeypatch.setattr(mod, "FinmindAsyncSessionLocal", SessionLocal)
        monkeypatch.setattr(mod, "finmind_engine", finmind_engine)
    yield
