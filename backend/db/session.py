from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from config import settings

# SQLite (used by tests) uses StaticPool and rejects server-pool kwargs;
# every other dialect (postgres in prod) wants the connection-pool tuning.
_engine_kwargs: dict = {"echo": settings.DEBUG}
if not settings.DATABASE_URL.startswith("sqlite"):
    _engine_kwargs.update(pool_pre_ping=True, pool_size=10, max_overflow=20)

engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_db_session_factory():
    """Return a callable that creates a fresh `AsyncSession`.

    Used by detached background tasks (e.g. `asyncio.create_task` workers
    that outlive the originating HTTP request) which need a session with
    a lifetime independent of `Depends(get_db)`. The dependency form
    lets tests substitute the in-memory test sessionmaker via
    `app.dependency_overrides`, so background-task code can be exercised
    against the same in-memory schema as the rest of the test suite.
    """
    return AsyncSessionLocal
