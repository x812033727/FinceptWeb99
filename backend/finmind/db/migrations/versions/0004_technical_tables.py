"""technical tables — ohlcv_daily + adj + per + day_trade + index + ...

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-03

Phase 1B. Per-symbol daily time-series tables that drive the
technical / price-history surface area. Hypertable-promotes the
ones with material row volume."""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
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


def _create_per_symbol_daily(
    name: str, value_cols: list[sa.Column], source_len: int = 16
) -> None:
    """Helper — every Shape-A table (PK market+symbol+ts, Numeric value
    cols) gets the same boilerplate. Reduces 7 nearly-identical
    create_table calls to one schema spec per dataset.

    `source_len` lets ohlcv_daily widen its `source` column to 24 chars
    (mirrors the main app's table) without a downstream alter_column
    that SQLite would reject."""
    op.create_table(
        name,
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        *value_cols,
        sa.Column("source", sa.String(length=source_len), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("market", "symbol", "ts", name=f"pk_{name}"),
    )
    op.create_index(
        f"ix_{name}_symbol_ts",
        name,
        ["symbol", sa.text("ts DESC")],
    )
    if _has_timescaledb():
        op.execute(
            f"SELECT create_hypertable('{name}', 'ts', "
            f"chunk_time_interval => INTERVAL '1 month', "
            f"if_not_exists => TRUE)"
        )


def upgrade() -> None:
    _create_per_symbol_daily(
        "ohlcv_daily",
        [
            sa.Column("open", sa.Numeric(18, 6), nullable=True),
            sa.Column("high", sa.Numeric(18, 6), nullable=True),
            sa.Column("low", sa.Numeric(18, 6), nullable=True),
            sa.Column("close", sa.Numeric(18, 6), nullable=True),
            sa.Column("volume", sa.BigInteger(), nullable=True),
        ],
        source_len=24,  # mirrors the main app's ohlcv_daily.source
    )

    _create_per_symbol_daily(
        "tw_stock_price_adj",
        [
            sa.Column("adj_close", sa.Numeric(18, 6), nullable=True),
            sa.Column("adj_factor", sa.Numeric(18, 8), nullable=True),
        ],
    )
    _create_per_symbol_daily(
        "tw_stock_per_daily",
        [
            sa.Column("per", sa.Numeric(12, 4), nullable=True),
            sa.Column("pbr", sa.Numeric(12, 4), nullable=True),
            sa.Column("dividend_yield", sa.Numeric(8, 4), nullable=True),
        ],
    )
    _create_per_symbol_daily(
        "tw_day_trade_daily",
        [
            sa.Column("buy_volume", sa.BigInteger(), nullable=True),
            sa.Column("sell_volume", sa.BigInteger(), nullable=True),
            sa.Column("buy_amount", sa.Numeric(20, 2), nullable=True),
            sa.Column("sell_amount", sa.Numeric(20, 2), nullable=True),
        ],
    )
    _create_per_symbol_daily(
        "tw_total_return_index",
        [sa.Column("value", sa.Numeric(18, 6), nullable=True)],
    )
    _create_per_symbol_daily(
        "tw_price_limit_daily",
        [
            sa.Column("upper_limit", sa.Numeric(18, 6), nullable=True),
            sa.Column("lower_limit", sa.Numeric(18, 6), nullable=True),
        ],
    )
    _create_per_symbol_daily(
        "tw_market_value_weight",
        [
            sa.Column("weight", sa.Numeric(12, 8), nullable=True),
            sa.Column("market_cap", sa.Numeric(24, 2), nullable=True),
        ],
    )

    # Sparse event table — different shape from the helper.
    op.create_table(
        "tw_suspended",
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("suspended_at", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("resumed_at", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("symbol", "suspended_at", name="pk_tw_suspended"),
    )


def downgrade() -> None:
    for name in (
        "tw_suspended",
        "tw_market_value_weight",
        "tw_price_limit_daily",
        "tw_total_return_index",
        "tw_day_trade_daily",
        "tw_stock_per_daily",
        "tw_stock_price_adj",
        "ohlcv_daily",
    ):
        op.drop_index(f"ix_{name}_symbol_ts", table_name=name, if_exists=True)
        op.drop_table(name)
