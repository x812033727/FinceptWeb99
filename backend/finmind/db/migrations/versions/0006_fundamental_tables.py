"""fundamental tables — income / balance / cash flow / revenue / market value

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-03

Phase 1D. Quarterly statements use a JSONB `raw` column for the
~120-line-item long tail plus typed canonical columns for the
metrics queries actually filter on (revenue, net_income, eps, etc.).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

revision: str = "0006"
down_revision: str | None = "0005"
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


def _json_col(name: str) -> sa.Column:
    return sa.Column(
        name, JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )


def _quarterly_statement(name: str, value_cols: list[sa.Column]) -> None:
    op.create_table(
        name,
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("period", sa.String(length=8), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=True),
        *value_cols,
        _json_col("raw"),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("symbol", "period", name=f"pk_{name}"),
    )
    op.create_index(
        f"ix_{name}_period_end",
        name,
        [sa.text("period_end DESC")],
    )


def upgrade() -> None:
    _quarterly_statement(
        "tw_income_statement",
        [
            sa.Column("revenue", sa.Numeric(24, 2), nullable=True),
            sa.Column("operating_income", sa.Numeric(24, 2), nullable=True),
            sa.Column("net_income", sa.Numeric(24, 2), nullable=True),
            sa.Column("eps", sa.Numeric(12, 4), nullable=True),
        ],
    )
    _quarterly_statement(
        "tw_balance_sheet",
        [
            sa.Column("total_assets", sa.Numeric(24, 2), nullable=True),
            sa.Column("total_liabilities", sa.Numeric(24, 2), nullable=True),
            sa.Column("total_equity", sa.Numeric(24, 2), nullable=True),
            sa.Column("cash", sa.Numeric(24, 2), nullable=True),
        ],
    )
    _quarterly_statement(
        "tw_cash_flow",
        [
            sa.Column("operating_cash_flow", sa.Numeric(24, 2), nullable=True),
            sa.Column("investing_cash_flow", sa.Numeric(24, 2), nullable=True),
            sa.Column("financing_cash_flow", sa.Numeric(24, 2), nullable=True),
            sa.Column("free_cash_flow", sa.Numeric(24, 2), nullable=True),
        ],
    )

    op.create_table(
        "tw_revenue_monthly",
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("revenue", sa.Numeric(24, 2), nullable=True),
        sa.Column("revenue_yoy", sa.Numeric(8, 4), nullable=True),
        sa.Column("revenue_mom", sa.Numeric(8, 4), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("symbol", "ts", name="pk_tw_revenue_monthly"),
    )
    op.create_index(
        "ix_tw_revenue_monthly_ts",
        "tw_revenue_monthly",
        [sa.text("ts DESC")],
    )

    op.create_table(
        "tw_business_indicator",
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("signal", sa.String(length=8), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("ts", name="pk_tw_business_indicator"),
    )

    op.create_table(
        "tw_market_value",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("market_cap", sa.Numeric(24, 2), nullable=True),
        sa.Column("issued_shares", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("market", "symbol", "ts", name="pk_tw_market_value"),
    )
    op.create_index(
        "ix_tw_market_value_symbol_ts",
        "tw_market_value",
        ["symbol", sa.text("ts DESC")],
    )
    if _has_timescaledb():
        op.execute(
            "SELECT create_hypertable('tw_market_value', 'ts', "
            "chunk_time_interval => INTERVAL '1 month', "
            "if_not_exists => TRUE)"
        )


def downgrade() -> None:
    for name in (
        "tw_market_value",
        "tw_business_indicator",
        "tw_revenue_monthly",
        "tw_cash_flow",
        "tw_balance_sheet",
        "tw_income_statement",
    ):
        try:
            op.drop_index(f"ix_{name}_period_end", table_name=name)
        except Exception:
            pass
        try:
            op.drop_index(f"ix_{name}_symbol_ts", table_name=name)
        except Exception:
            pass
        try:
            op.drop_index(f"ix_{name}_ts", table_name=name)
        except Exception:
            pass
        op.drop_table(name)
