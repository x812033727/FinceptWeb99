"""ohlcv_daily — daily K-line archive table

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-28

Phase 1 of the scheduled-fetch-into-Postgres subsystem (see plan):
adds a single OHLCV table that scheduled tasks populate from connectors,
and that the read-side service consults between Redis and the upstream
waterfall.

Schema choices:
  - Composite PK (market, symbol, ts) so daily ingest is an idempotent
    UPSERT instead of needing a SERIAL id.
  - `ts` (date) is in the PK so TimescaleDB's "partitioning column must
    be in every UNIQUE index" rule is satisfied when we promote the
    table to a hypertable.
  - `market` column is included from day 1 (default 'TW' in this PR)
    so future US/Crypto extensions don't need a schema migration.
  - Hypertable conversion is wrapped in a TimescaleDB-presence check so
    plain PostgreSQL and SQLite (used by tests) work unchanged.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _has_timescaledb() -> bool:
    if not _is_postgres():
        return False
    return bool(op.get_bind().exec_driver_sql(
        "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"
    ).scalar())


def upgrade() -> None:
    op.create_table(
        "ohlcv_daily",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(18, 6), nullable=True),
        sa.Column("high", sa.Numeric(18, 6), nullable=True),
        sa.Column("low", sa.Numeric(18, 6), nullable=True),
        sa.Column("close", sa.Numeric(18, 6), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("market", "symbol", "ts", name="pk_ohlcv_daily"),
    )
    op.create_index(
        "ix_ohlcv_daily_lookup",
        "ohlcv_daily",
        ["market", "symbol", "ts"],
        unique=False,
    )

    if _has_timescaledb():
        op.execute(
            "SELECT create_hypertable("
            "'ohlcv_daily', 'ts', "
            "chunk_time_interval => INTERVAL '1 month', "
            "if_not_exists => TRUE)"
        )


def downgrade() -> None:
    op.drop_index("ix_ohlcv_daily_lookup", table_name="ohlcv_daily")
    op.drop_table("ohlcv_daily")
