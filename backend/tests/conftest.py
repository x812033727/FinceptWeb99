"""
Shared pytest fixtures.

Tests run against an in-memory SQLite database and mock Redis so no
external services are needed in CI.
"""
# Must set env before any backend module is imported — config.py instantiates
# Settings() at import time and validates JWT_SECRET_KEY strength.
import os

os.environ.setdefault("JWT_SECRET_KEY", "pytest-local-secret-key-32chars!!")

# Swap passlib's bcrypt scheme for pbkdf2_sha256 in tests. bcrypt 3.2.2 pulls in
# a cffi C-extension (`_cffi_backend`) that is missing in some dev environments
# and triggers `pyo3_runtime.PanicException` at module import. pbkdf2_sha256 is
# pure-Python (hashlib) so it works everywhere. Production code path unchanged.
import passlib.context as _passlib_context  # noqa: E402

_orig_cryptcontext_init = _passlib_context.CryptContext.__init__


def _patched_cryptcontext_init(self, *args, **kwargs):
    schemes = kwargs.get("schemes") if "schemes" in kwargs else (args[0] if args else None)
    if schemes and "bcrypt" in schemes:
        replaced = ["pbkdf2_sha256" if s == "bcrypt" else s for s in schemes]
        if "schemes" in kwargs:
            kwargs["schemes"] = replaced
        else:
            args = (replaced,) + args[1:]
    _orig_cryptcontext_init(self, *args, **kwargs)


_passlib_context.CryptContext.__init__ = _patched_cryptcontext_init

import asyncio  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Import every model so Base.metadata.create_all registers the full schema.
# Without this, tests that exercise one table transitively depend on another
# model being imported by a sibling test — leading to FK-resolution failures
# when tests run in isolation.
import models.alert  # noqa: E402,F401
import models.discussion  # noqa: E402,F401
import models.discussion_auto_run_config  # noqa: E402,F401
import models.discussion_round_context  # noqa: E402,F401
import models.fundamentals_snapshot  # noqa: E402,F401
import models.llm_provider_key  # noqa: E402,F401
import models.llm_usage_event  # noqa: E402,F401
import models.market_provider_key  # noqa: E402,F401
import models.news_article  # noqa: E402,F401
import models.ohlcv_daily  # noqa: E402,F401
import models.persona_override  # noqa: E402,F401
import models.portfolio  # noqa: E402,F401
import models.quote_snapshot  # noqa: E402,F401
import models.runtime_setting  # noqa: E402,F401
import models.system_task_config  # noqa: E402,F401
import models.tw_chip_metrics  # noqa: E402,F401
import models.tw_revenue_monthly  # noqa: E402,F401
import models.tw_stock_buyback  # noqa: E402,F401
import models.user  # noqa: E402,F401
import models.user_llm_provider_key  # noqa: E402,F401
import models.watchlist  # noqa: E402,F401
from db.base import Base  # noqa: E402
from db.session import get_db, get_db_session_factory  # noqa: E402

# Disable slowapi rate limiter — tests exercise endpoints in tight loops
# and would otherwise trip the 5/min register cap, etc.
from limiter import limiter  # noqa: E402

limiter.enabled = False

# ── in-memory SQLite engine ───────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

from sqlalchemy.pool import StaticPool  # noqa: E402

# StaticPool keeps a single connection alive for the engine's lifetime so
# every session — including detached background tasks running concurrently
# with the request's session — sees the same in-memory schema. Without it
# each `TestSessionLocal()` checkout could land on a fresh aiosqlite
# connection with its own (empty) in-memory DB.
test_engine = create_async_engine(
    TEST_DB_URL,
    echo=False,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest_asyncio.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture(autouse=True)
async def _truncate_db_between_tests():
    """Wipe every table after each test so writes from one test don't
    contaminate the next.

    Several tests use `*_autosession` helpers (`upsert_ohlcv_bars`,
    `record_health`, `insert_quote_snapshot`, …) that open their own
    session and commit, persisting rows beyond the test's own
    `db_session` fixture. Without explicit cleanup, the second test
    sees the first test's seed data and assertions fail
    (e.g. `test_persist_scoreboard_partial_window_returns_false`
    expects only 2 bars for `2330` but inherits the 5 bars seeded by
    the previous `_full_window` case).

    DELETE per table is fast on in-memory SQLite and runs in
    dependency order so foreign keys don't fight us.
    """
    yield
    async with test_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest_asyncio.fixture
async def db_session():
    async with TestSessionLocal() as session:
        yield session


# ── point every module-level `AsyncSessionLocal` at the test sessionmaker ──
#
# Production code uses `from db.session import AsyncSessionLocal` and then
# `async with AsyncSessionLocal() as db:` — bypassing FastAPI's dependency
# injection. Without this fixture those autosession calls reach the real
# DB and (depending on env) either fail or silently return [] caught by an
# upstream `except Exception`, causing tests to fall through to live
# upstream connectors and assertion mismatches against unmocked external
# data (e.g. `test_compute_scoreboard_full_window` expecting open=600 but
# getting today's TSMC tick).
#
# Patches both `db.session.AsyncSessionLocal` (for lazy imports inside
# functions) and the bound copies in modules that imported it at top
# level (the binding is fixed at import time so changing the source
# module's attribute alone isn't enough).
@pytest.fixture(autouse=True)
def _override_async_session_local(monkeypatch):
    import sys
    monkeypatch.setattr("db.session.AsyncSessionLocal", TestSessionLocal)
    for mod_name, mod in list(sys.modules.items()):
        if mod is None or mod_name == "db.session":
            continue
        if getattr(mod, "AsyncSessionLocal", None) is not None:
            try:
                monkeypatch.setattr(f"{mod_name}.AsyncSessionLocal", TestSessionLocal)
            except (AttributeError, ImportError):
                pass


# ── mock Redis ────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_redis():
    with patch("cache.redis_cache.get_redis") as mock:
        r = AsyncMock()
        r.get.return_value = None
        r.set.return_value = True
        r.delete.return_value = 1
        r.incr.return_value = 1
        r.expire.return_value = True
        r.ping.return_value = True
        mock.return_value = r
        yield r


# ── FastAPI test client ───────────────────────────────────────────

@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    from main import app

    async def override_db():
        yield db_session

    # Detached background tasks (e.g. discussion round runner) ask for a
    # session factory via Depends(get_db_session_factory). Point them at
    # the in-memory test sessionmaker so they hit the same schema as the
    # rest of the test — without this the bg task crashes with "no such
    # table" because production AsyncSessionLocal targets the real DB.
    def override_session_factory():
        return TestSessionLocal

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_db_session_factory] = override_session_factory

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
