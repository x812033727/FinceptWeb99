"""Async SQLAlchemy session factory for the FinMind clone DB.

Same pool tuning as `db.session` (the main app). Binds to either
`FINMIND_DATABASE_URL` (default — separate `postgres_finmind` container)
or the main app's `DATABASE_URL` with a `finmind` Postgres schema
(when `FINMIND_USE_MAIN_DB=true` — small / managed deploys that can't
run a second container). Exposed as `FinmindAsyncSessionLocal` so a
stray import is a loud name collision rather than silently writing
into the wrong database.
"""
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from finmind.config import finmind_settings

_effective_url = finmind_settings.effective_database_url

_engine_kwargs: dict = {"echo": finmind_settings.DEBUG}
if not _effective_url.startswith("sqlite"):
    _engine_kwargs.update(pool_pre_ping=True, pool_size=10, max_overflow=20)

finmind_engine = create_async_engine(_effective_url, **_engine_kwargs)


# When sharing the main DB, force every checked-out connection to
# search the `finmind` schema first so unqualified table references
# (`SELECT * FROM dataset_sources`) resolve to `finmind.dataset_sources`
# rather than `public.dataset_sources`. Same trick the Alembic env uses;
# kept here so app-runtime queries don't have to qualify either.
_schema = finmind_settings.schema
if _schema is not None:

    @event.listens_for(finmind_engine.sync_engine, "connect")
    def _set_finmind_search_path(dbapi_conn, _conn_record) -> None:
        cur = dbapi_conn.cursor()
        try:
            cur.execute(f'SET search_path TO "{_schema}", public')
        finally:
            cur.close()

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
