"""chip tables — margin / institutional / lending / broker daily report / ...

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-03

Phase 1C. Chip / 籌碼 surface — 14 tables covering both per-symbol
and market-wide daily aggregates plus the heavy 分點 hypertable
(`tw_broker_daily_report`) that dominates total disk usage.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
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


def _per_symbol_daily(name: str, value_cols: list[sa.Column]) -> None:
    op.create_table(
        name,
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        *value_cols,
        sa.Column("source", sa.String(length=16), nullable=False),
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


def _market_wide_daily(name: str, value_cols: list[sa.Column]) -> None:
    op.create_table(
        name,
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        *value_cols,
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("market", "ts", name=f"pk_{name}"),
    )
    if _has_timescaledb():
        op.execute(
            f"SELECT create_hypertable('{name}', 'ts', "
            f"chunk_time_interval => INTERVAL '6 months', "
            f"if_not_exists => TRUE)"
        )


def upgrade() -> None:
    # ── per-symbol daily ────
    _per_symbol_daily(
        "tw_margin_daily",
        [
            sa.Column("margin_purchase", sa.BigInteger(), nullable=True),
            sa.Column("margin_sale", sa.BigInteger(), nullable=True),
            sa.Column("margin_balance", sa.BigInteger(), nullable=True),
            sa.Column("short_sale", sa.BigInteger(), nullable=True),
            sa.Column("short_cover", sa.BigInteger(), nullable=True),
            sa.Column("short_balance", sa.BigInteger(), nullable=True),
        ],
    )
    _per_symbol_daily(
        "tw_institutional_daily",
        [
            sa.Column("foreign_buy", sa.BigInteger(), nullable=True),
            sa.Column("foreign_sell", sa.BigInteger(), nullable=True),
            sa.Column("sitc_buy", sa.BigInteger(), nullable=True),
            sa.Column("sitc_sell", sa.BigInteger(), nullable=True),
            sa.Column("dealer_buy", sa.BigInteger(), nullable=True),
            sa.Column("dealer_sell", sa.BigInteger(), nullable=True),
        ],
    )
    _per_symbol_daily(
        "tw_day_trade_fee",
        [sa.Column("fee_rate", sa.Numeric(8, 4), nullable=True)],
    )
    _per_symbol_daily(
        "tw_foreign_shareholding",
        [
            sa.Column("foreign_holding_pct", sa.Numeric(8, 4), nullable=True),
            sa.Column("foreign_holding_shares", sa.BigInteger(), nullable=True),
            sa.Column("available_shares", sa.BigInteger(), nullable=True),
        ],
    )

    # tw_securities_lending — composite PK with transaction_type, hand-rolled.
    op.create_table(
        "tw_securities_lending",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("transaction_type", sa.String(length=32), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("fee_rate", sa.Numeric(8, 4), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "ts", "transaction_type",
            name="pk_tw_securities_lending",
        ),
    )

    # tw_holdings_aggregates — TDCC weekly, bracket in PK.
    op.create_table(
        "tw_holdings_aggregates",
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("bracket", sa.String(length=32), nullable=False),
        sa.Column("holders", sa.BigInteger(), nullable=True),
        sa.Column("shares", sa.BigInteger(), nullable=True),
        sa.Column("pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "symbol", "ts", "bracket", name="pk_tw_holdings_aggregates"
        ),
    )

    # ── market-wide daily ────
    _market_wide_daily(
        "tw_total_margin_daily",
        [
            sa.Column("margin_balance", sa.BigInteger(), nullable=True),
            sa.Column("margin_purchase", sa.BigInteger(), nullable=True),
            sa.Column("margin_sale", sa.BigInteger(), nullable=True),
            sa.Column("short_balance", sa.BigInteger(), nullable=True),
            sa.Column("short_sale", sa.BigInteger(), nullable=True),
            sa.Column("short_cover", sa.BigInteger(), nullable=True),
        ],
    )
    _market_wide_daily(
        "tw_total_inst_daily",
        [
            sa.Column("foreign_net", sa.BigInteger(), nullable=True),
            sa.Column("sitc_net", sa.BigInteger(), nullable=True),
            sa.Column("dealer_net", sa.BigInteger(), nullable=True),
        ],
    )
    _market_wide_daily(
        "tw_short_sale_balance_daily",
        [
            sa.Column("short_balance", sa.BigInteger(), nullable=True),
            sa.Column("short_quota", sa.BigInteger(), nullable=True),
        ],
    )
    _market_wide_daily(
        "tw_margin_maintenance",
        [sa.Column("maintenance_pct", sa.Numeric(8, 4), nullable=True)],
    )
    _market_wide_daily(
        "tw_loan_collateral",
        [sa.Column("collateral_balance", sa.Numeric(24, 2), nullable=True)],
    )
    _market_wide_daily(
        "tw_govt_bank_flow",
        [
            sa.Column("buy_amount", sa.Numeric(20, 2), nullable=True),
            sa.Column("sell_amount", sa.Numeric(20, 2), nullable=True),
            sa.Column("net_amount", sa.Numeric(20, 2), nullable=True),
        ],
    )

    # ── event-shaped ────
    op.create_table(
        "tw_short_sale_suspension",
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("suspended_at", sa.Date(), nullable=False),
        sa.Column("resumed_at", sa.Date(), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("symbol", "suspended_at", name="pk_tw_short_sale_suspension"),
    )

    op.create_table(
        "tw_block_trade",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("price", sa.Numeric(18, 6), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("amount", sa.Numeric(24, 2), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("market", "symbol", "ts", "seq", name="pk_tw_block_trade"),
    )

    op.create_table(
        "tw_disposition",
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.Date(), nullable=False),
        sa.Column("ended_at", sa.Date(), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("symbol", "started_at", name="pk_tw_disposition"),
    )

    # ── heavy hypertable: 分點 ────
    op.create_table(
        "tw_broker_daily_report",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("broker_id", sa.String(length=16), nullable=False),
        sa.Column("price", sa.Numeric(18, 6), nullable=False),
        sa.Column("buy_volume", sa.BigInteger(), nullable=True),
        sa.Column("sell_volume", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "ts", "broker_id", "price",
            name="pk_tw_broker_daily_report",
        ),
    )
    op.create_index(
        "ix_tw_broker_daily_report_lookup",
        "tw_broker_daily_report",
        ["symbol", "ts", "broker_id"],
    )
    if _has_timescaledb():
        op.execute(
            "SELECT create_hypertable('tw_broker_daily_report', 'ts', "
            "chunk_time_interval => INTERVAL '1 week', "
            "if_not_exists => TRUE)"
        )


def downgrade() -> None:
    for name in (
        "tw_broker_daily_report",
        "tw_disposition",
        "tw_block_trade",
        "tw_short_sale_suspension",
        "tw_govt_bank_flow",
        "tw_loan_collateral",
        "tw_margin_maintenance",
        "tw_short_sale_balance_daily",
        "tw_total_inst_daily",
        "tw_total_margin_daily",
        "tw_holdings_aggregates",
        "tw_securities_lending",
        "tw_foreign_shareholding",
        "tw_day_trade_fee",
        "tw_institutional_daily",
        "tw_margin_daily",
    ):
        try:
            op.drop_index(f"ix_{name}_symbol_ts", table_name=name)
        except Exception:
            pass
        op.drop_table(name)
