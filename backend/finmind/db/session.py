"""Async SQLAlchemy session factory for the FinMind clone DB.

Same pool tuning as `db.session` (the main app). Binds to either
the main app's `DATABASE_URL` with a `finmind` Postgres schema
(when `FINMIND_USE_MAIN_DB=true` — the default since PR #313, works
out-of-box for any single-Postgres deploy) or `FINMIND_DATABASE_URL`
(when `FINMIND_USE_MAIN_DB=false` — explicit opt-in for ops that
want a separate `postgres_finmind` container at the database level).
Exposed as `FinmindAsyncSessionLocal` so a stray import is a loud
name collision rather than silently writing into the wrong database.
"""
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from finmind.config import finmind_settings

_effective_url = finmind_settings.effective_database_url

_engine_kwargs: dict = {"echo": finmind_settings.DEBUG}
if not _effective_url.startswith("sqlite"):
    # `pool_pre_ping=True` is unsafe with asyncpg when an ORM lazy-load
    # triggers a connection checkout from a non-greenlet stack frame:
    # the pre-ping calls `connection.ping()`, which on asyncpg needs to
    # `await` the underlying coroutine via SQLAlchemy's greenlet bridge,
    # and dies with `MissingGreenlet: greenlet_spawn has not been
    # called` mid-backfill. Use `pool_recycle` instead — same goal
    # (drop connections older than the typical idle timeout) without
    # any per-checkout IO.
    _engine_kwargs.update(pool_recycle=1800, pool_size=10, max_overflow=20)

# When sharing the main DB, force every checked-out connection to
# search the `finmind` schema first so unqualified table references
# (`SELECT * FROM dataset_sources`) resolve to `finmind.dataset_sources`
# rather than `public.dataset_sources`. The previous approach used a
# `@event.listens_for(sync_engine, "connect")` listener that ran a
# `SET search_path` per fresh DBAPI connection, but that path is
# unreliable on asyncpg — fresh connections sometimes started a query
# before the listener's cursor had executed, which surfaced as
# `relation "dataset_sources" does not exist` mid-backfill. asyncpg
# accepts `server_settings` directly via `connect_args`, which it
# applies as part of the connection handshake, so the schema lookup is
# in place before the first query and never races.
_schema = finmind_settings.schema
if _schema is not None and not _effective_url.startswith("sqlite"):
    _engine_kwargs.setdefault("connect_args", {})["server_settings"] = {
        "search_path": f"{_schema},public",
    }

finmind_engine = create_async_engine(_effective_url, **_engine_kwargs)

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
