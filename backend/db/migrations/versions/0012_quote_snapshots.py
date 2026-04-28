"""quote_snapshots — periodic last-price archive

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-28

Phase 2 of the scheduled-fetch subsystem. Each TW quote refresh writes
one row here so the read path can serve a recent last-price even when
the upstream waterfall is unavailable, and operators can reconstruct
intraday tick history without keeping every tick in Redis.

Sized for ~50 actively-watched symbols × 16 writes/day during the TW
4.5 h session ≈ 1k rows/day (3M/yr at full load). A daily prune task
keeps only the last 30 days; longer retention is opt-in via TimescaleDB
compression.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
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
        "quote_snapshots",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("change_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("prev_close", sa.Numeric(18, 6), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.PrimaryKeyConstraint("market", "symbol", "ts", name="pk_quote_snapshots"),
    )
    op.create_index(
        "ix_quote_snapshots_lookup",
        "quote_snapshots",
        ["market", "symbol", "ts"],
        unique=False,
    )

    if _has_timescaledb():
        op.execute(
            "SELECT create_hypertable("
            "'quote_snapshots', 'ts', "
            "chunk_time_interval => INTERVAL '1 day', "
            "if_not_exists => TRUE)"
        )


def downgrade() -> None:
    op.drop_index("ix_quote_snapshots_lookup", table_name="quote_snapshots")
    op.drop_table("quote_snapshots")
