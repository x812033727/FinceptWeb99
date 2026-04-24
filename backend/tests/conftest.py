"""
Shared pytest fixtures.

Tests run against an in-memory SQLite database and mock Redis so no
external services are needed in CI.
"""
import asyncio
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from db.base import Base
from db.session import get_db

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
