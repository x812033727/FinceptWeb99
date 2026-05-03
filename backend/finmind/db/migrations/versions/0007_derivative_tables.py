"""derivative tables — futures + options + 三大法人 + 大額交易人

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-03

Phase 1E. 12 tables covering both futures and options across daily,
institutional, large-trader OI, settlement, and dealer-volume axes.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
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


def _ts_cols() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def upgrade() -> None:
    # tw_futures_daily — PK (contract, ts)
    op.create_table(
        "tw_futures_daily",
        sa.Column("contract", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(18, 6), nullable=True),
        sa.Column("high", sa.Numeric(18, 6), nullable=True),
        sa.Column("low", sa.Numeric(18, 6), nullable=True),
        sa.Column("close", sa.Numeric(18, 6), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("open_interest", sa.BigInteger(), nullable=True),
        sa.Column("settlement_price", sa.Numeric(18, 6), nullable=True),
        *_ts_cols(),
        sa.PrimaryKeyConstraint("contract", "ts", name="pk_tw_futures_daily"),
    )
    if _has_timescaledb():
        op.execute(
            "SELECT create_hypertable('tw_futures_daily', 'ts', "
            "chunk_time_interval => INTERVAL '3 months', if_not_exists => TRUE)"
        )

    # tw_option_daily — PK (contract, strike, call_put, ts)
    op.create_table(
        "tw_option_daily",
        sa.Column("contract", sa.String(length=20), nullable=False),
        sa.Column("strike", sa.Numeric(18, 6), nullable=False),
        sa.Column("call_put", sa.String(length=4), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(18, 6), nullable=True),
        sa.Column("high", sa.Numeric(18, 6), nullable=True),
        sa.Column("low", sa.Numeric(18, 6), nullable=True),
        sa.Column("close", sa.Numeric(18, 6), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("open_interest", sa.BigInteger(), nullable=True),
        *_ts_cols(),
        sa.PrimaryKeyConstraint(
            "contract", "strike", "call_put", "ts", name="pk_tw_option_daily"
        ),
    )
    op.create_index(
        "ix_tw_option_daily_contract_ts",
        "tw_option_daily",
        ["contract", sa.text("ts DESC")],
    )
    if _has_timescaledb():
        op.execute(
            "SELECT create_hypertable('tw_option_daily', 'ts', "
            "chunk_time_interval => INTERVAL '1 month', if_not_exists => TRUE)"
        )

    op.create_table(
        "tw_futures_inst_daily",
        sa.Column("contract", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("session", sa.String(length=8), nullable=False),
        sa.Column("foreign_long_open_interest", sa.BigInteger(), nullable=True),
        sa.Column("foreign_short_open_interest", sa.BigInteger(), nullable=True),
        sa.Column("sitc_long_open_interest", sa.BigInteger(), nullable=True),
        sa.Column("sitc_short_open_interest", sa.BigInteger(), nullable=True),
        sa.Column("dealer_long_open_interest", sa.BigInteger(), nullable=True),
        sa.Column("dealer_short_open_interest", sa.BigInteger(), nullable=True),
        *_ts_cols(),
        sa.PrimaryKeyConstraint(
            "contract", "ts", "session", name="pk_tw_futures_inst_daily"
        ),
    )

    op.create_table(
        "tw_option_inst_daily",
        sa.Column("contract", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("session", sa.String(length=8), nullable=False),
        sa.Column("call_put", sa.String(length=4), nullable=False),
        sa.Column("foreign_long_open_interest", sa.BigInteger(), nullable=True),
        sa.Column("foreign_short_open_interest", sa.BigInteger(), nullable=True),
        sa.Column("sitc_long_open_interest", sa.BigInteger(), nullable=True),
        sa.Column("sitc_short_open_interest", sa.BigInteger(), nullable=True),
        sa.Column("dealer_long_open_interest", sa.BigInteger(), nullable=True),
        sa.Column("dealer_short_open_interest", sa.BigInteger(), nullable=True),
        *_ts_cols(),
        sa.PrimaryKeyConstraint(
            "contract", "ts", "session", "call_put",
            name="pk_tw_option_inst_daily",
        ),
    )

    op.create_table(
        "tw_futures_oi_largetraders",
        sa.Column("contract", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("rank", sa.String(length=16), nullable=False),
        sa.Column("long_oi", sa.BigInteger(), nullable=True),
        sa.Column("short_oi", sa.BigInteger(), nullable=True),
        sa.Column("long_oi_special", sa.BigInteger(), nullable=True),
        sa.Column("short_oi_special", sa.BigInteger(), nullable=True),
        *_ts_cols(),
        sa.PrimaryKeyConstraint(
            "contract", "ts", "rank", name="pk_tw_futures_oi_largetraders"
        ),
    )

    op.create_table(
        "tw_option_oi_largetraders",
        sa.Column("contract", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("call_put", sa.String(length=4), nullable=False),
        sa.Column("rank", sa.String(length=16), nullable=False),
        sa.Column("long_oi", sa.BigInteger(), nullable=True),
        sa.Column("short_oi", sa.BigInteger(), nullable=True),
        *_ts_cols(),
        sa.PrimaryKeyConstraint(
            "contract", "ts", "call_put", "rank",
            name="pk_tw_option_oi_largetraders",
        ),
    )

    op.create_table(
        "tw_futures_settlement",
        sa.Column("contract", sa.String(length=20), nullable=False),
        sa.Column("settlement_date", sa.Date(), nullable=False),
        sa.Column("final_settlement_price", sa.Numeric(18, 6), nullable=True),
        *_ts_cols(),
        sa.PrimaryKeyConstraint(
            "contract", "settlement_date", name="pk_tw_futures_settlement"
        ),
    )

    op.create_table(
        "tw_option_settlement",
        sa.Column("contract", sa.String(length=20), nullable=False),
        sa.Column("settlement_date", sa.Date(), nullable=False),
        sa.Column("strike", sa.Numeric(18, 6), nullable=False),
        sa.Column("call_put", sa.String(length=4), nullable=False),
        sa.Column("final_settlement_price", sa.Numeric(18, 6), nullable=True),
        *_ts_cols(),
        sa.PrimaryKeyConstraint(
            "contract", "settlement_date", "strike", "call_put",
            name="pk_tw_option_settlement",
        ),
    )

    op.create_table(
        "tw_futopt_daily_info",
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("futures_volume", sa.BigInteger(), nullable=True),
        sa.Column("options_volume", sa.BigInteger(), nullable=True),
        sa.Column("futures_open_interest", sa.BigInteger(), nullable=True),
        sa.Column("options_open_interest", sa.BigInteger(), nullable=True),
        *_ts_cols(),
        sa.PrimaryKeyConstraint("ts", name="pk_tw_futopt_daily_info"),
    )

    op.create_table(
        "tw_futures_dealer_volume",
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("dealer_id", sa.String(length=16), nullable=False),
        sa.Column("contract", sa.String(length=20), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        *_ts_cols(),
        sa.PrimaryKeyConstraint(
            "ts", "dealer_id", "contract", name="pk_tw_futures_dealer_volume"
        ),
    )

    op.create_table(
        "tw_option_dealer_volume",
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("dealer_id", sa.String(length=16), nullable=False),
        sa.Column("contract", sa.String(length=20), nullable=False),
        sa.Column("call_put", sa.String(length=4), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        *_ts_cols(),
        sa.PrimaryKeyConstraint(
            "ts", "dealer_id", "contract", "call_put",
            name="pk_tw_option_dealer_volume",
        ),
    )

    op.create_table(
        "tw_futures_spread",
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("spread_pair", sa.String(length=40), nullable=False),
        sa.Column("open", sa.Numeric(18, 6), nullable=True),
        sa.Column("close", sa.Numeric(18, 6), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        *_ts_cols(),
        sa.PrimaryKeyConstraint("ts", "spread_pair", name="pk_tw_futures_spread"),
    )


def downgrade() -> None:
    for name in (
        "tw_futures_spread",
        "tw_option_dealer_volume",
        "tw_futures_dealer_volume",
        "tw_futopt_daily_info",
        "tw_option_settlement",
        "tw_futures_settlement",
        "tw_option_oi_largetraders",
        "tw_futures_oi_largetraders",
        "tw_option_inst_daily",
        "tw_futures_inst_daily",
        "tw_option_daily",
        "tw_futures_daily",
    ):
        try:
            op.drop_index(f"ix_{name}_contract_ts", table_name=name)
        except Exception:
            pass
        op.drop_table(name)
