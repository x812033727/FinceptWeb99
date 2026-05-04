"""Alembic env for the FinMind clone DB.

Two key isolation choices vs. the main app's `db/migrations/env.py`:

  1. `version_table='alembic_version_finmind'` — even if FinMind tables
     ever co-exist with the main app's in the same physical Postgres,
     the two version histories never collide.

  2. Imports `finmind.db.base.Base` and the `finmind.models.*` modules
     only — `target_metadata` doesn't see the main app's tables, so
     autogenerate can't accidentally produce a migration that drops
     them.
"""
import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Path: env.py → migrations → db → finmind → backend
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from finmind.config import finmind_settings  # noqa: E402
from finmind.db.base import Base  # noqa: E402

# Single import — `finmind/models/__init__.py` re-exports every model
# module, so adding a new model under that package registers it here
# automatically without touching this file.
import finmind.models  # noqa: E402,F401

config = context.config
config.set_main_option(
    "sqlalchemy.url", finmind_settings.effective_database_url,
)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# TimescaleDB internal tables — exclude from autogenerate.
EXCLUDE_TABLES = {
    "_timescaledb_catalog",
    "_timescaledb_config",
    "_timescaledb_internal",
}


def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table" and name in EXCLUDE_TABLES:
        return False
    return True


_VERSION_TABLE = "alembic_version_finmind"
_SCHEMA = finmind_settings.schema  # None or 'finmind' when sharing main DB


def run_migrations_offline() -> None:
    context.configure(
        url=finmind_settings.effective_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
        version_table=_VERSION_TABLE,
        version_table_schema=_SCHEMA,
        include_schemas=_SCHEMA is not None,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # When sharing the main DB, ensure the `finmind` schema exists +
    # set search_path BEFORE any migration runs. Otherwise the first
    # `op.create_table('dataset_sources', ...)` lands in `public.`.
    if _SCHEMA is not None:
        from sqlalchemy import text as _text

        connection.execute(_text(f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}"'))
        connection.execute(_text(f'SET search_path TO "{_SCHEMA}", public'))

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
        transaction_per_migration=True,
        version_table=_VERSION_TABLE,
        version_table_schema=_SCHEMA,
        include_schemas=_SCHEMA is not None,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
