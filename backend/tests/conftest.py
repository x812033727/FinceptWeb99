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
import models.fundamentals_snapshot  # noqa: E402,F401
import models.llm_provider_key  # noqa: E402,F401
import models.llm_usage_event  # noqa: E402,F401
import models.market_provider_key  # noqa: E402,F401
import models.news_article  # noqa: E402,F401
import models.ohlcv_daily  # noqa: E402,F401
import models.persona_override  # noqa: E402,F401
import models.portfolio  # noqa: E402,F401
import models.quote_snapshot  # noqa: E402,F401
import models.user  # noqa: E402,F401
import models.user_llm_provider_key  # noqa: E402,F401
import models.watchlist  # noqa: E402,F401
from db.base import Base  # noqa: E402
from db.session import get_db  # noqa: E402

# Disable slowapi rate limiter — tests exercise endpoints in tight loops
# and would otherwise trip the 5/min register cap, etc.
from limiter import limiter  # noqa: E402

limiter.enabled = False

# ── in-memory SQLite engine ───────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DB_URL, echo=False)
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


@pytest_asyncio.fixture
async def db_session():
    async with TestSessionLocal() as session:
        yield session


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

    app.dependency_overrides[get_db] = override_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
