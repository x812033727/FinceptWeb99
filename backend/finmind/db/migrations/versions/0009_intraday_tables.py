"""intraday tables — tw_stock_minute + tw_stock_tick

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-03

Phase 1H. Two big hypertables — minute K (1m chunks daily) and tick
(1h chunks because the row volume per chunk would otherwise dwarf
PG's working set). Compression ratio ~18× and ~15× respectively
will be enabled in 0010.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_timescaledb() -> bool:
    if op.get_bind().dialect.name != "postgresql":
        return False
    return bool(
        op.get_bind()
        .exec_driver_sql(
            "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"
        )
        .scalar()
    )


def upgrade() -> None:
    op.create_table(
        "tw_stock_minute",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(18, 6), nullable=True),
        sa.Column("high", sa.Numeric(18, 6), nullable=True),
        sa.Column("low", sa.Numeric(18, 6), nullable=True),
        sa.Column("close", sa.Numeric(18, 6), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("market", "symbol", "ts", name="pk_tw_stock_minute"),
    )
    op.create_index(
        "ix_tw_stock_minute_symbol_ts",
        "tw_stock_minute",
        ["symbol", "ts"],
    )
    if _has_timescaledb():
        op.execute(
            "SELECT create_hypertable('tw_stock_minute', 'ts', "
            "chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE)"
        )

    op.create_table(
        "tw_stock_tick",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("price", sa.Numeric(18, 6), nullable=True),
        sa.Column("volume", sa.Integer(), nullable=True),
        sa.Column("bid", sa.Numeric(18, 6), nullable=True),
        sa.Column("ask", sa.Numeric(18, 6), nullable=True),
        sa.Column("tick_type", sa.String(length=2), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "ts", "seq", name="pk_tw_stock_tick"
        ),
    )
    op.create_index(
        "ix_tw_stock_tick_symbol_ts",
        "tw_stock_tick",
        ["symbol", "ts"],
    )
    if _has_timescaledb():
        op.execute(
            "SELECT create_hypertable('tw_stock_tick', 'ts', "
            "chunk_time_interval => INTERVAL '1 hour', if_not_exists => TRUE)"
        )


def downgrade() -> None:
    op.drop_index("ix_tw_stock_tick_symbol_ts", table_name="tw_stock_tick")
    op.drop_table("tw_stock_tick")
    op.drop_index("ix_tw_stock_minute_symbol_ts", table_name="tw_stock_minute")
    op.drop_table("tw_stock_minute")
