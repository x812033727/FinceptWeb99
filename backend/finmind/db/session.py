"""Async SQLAlchemy session factory for the FinMind clone DB.

Same pool tuning as `db.session` (the main app), just bound to
`FINMIND_DATABASE_URL` instead. Exposed as `FinmindAsyncSessionLocal`
to make the contrast with `AsyncSessionLocal` (main DB) impossible to
miss at call sites — a stray import is a loud name collision rather
than silently writing into the wrong database.
"""
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from finmind.config import finmind_settings

_engine_kwargs: dict = {"echo": finmind_settings.DEBUG}
if not finmind_settings.FINMIND_DATABASE_URL.startswith("sqlite"):
    _engine_kwargs.update(pool_pre_ping=True, pool_size=10, max_overflow=20)

finmind_engine = create_async_engine(
    finmind_settings.FINMIND_DATABASE_URL, **_engine_kwargs
)

FinmindAsyncSessionLocal = async_sessionmaker(
    bind=finmind_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_finmind_db() -> AsyncSession:
    """FastAPI dependency. Mirrors `db.session.get_db` semantics."""
    async with FinmindAsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_finmind_session_factory():
    """Callable returning a fresh `AsyncSession` — for detached background
    tasks (ingest workers) that outlive the originating HTTP request."""
    return FinmindAsyncSessionLocal
