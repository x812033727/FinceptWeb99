"""master tables — stock info, calendar, broker, industry chain, delisting

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-03

Phase 1A. Static / slowly-changing dimension tables. Not hypertables
(low row volume, queried by symbol not by time)."""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tw_stock_info",
        sa.Column("market", sa.String(length=8), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("name_zh", sa.String(length=64), nullable=True),
        sa.Column("industry_category", sa.String(length=32), nullable=True),
        sa.Column("listed_at", sa.Date(), nullable=True),
        sa.Column("is_warrant", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("market", "symbol", name="pk_tw_stock_info"),
    )
    op.create_index(
        "ix_tw_stock_info_industry",
        "tw_stock_info",
        ["industry_category"],
    )

    op.create_table(
        "tw_trading_calendar",
        sa.Column("market", sa.String(length=8), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("is_trading_day", sa.Boolean(), nullable=False),
        sa.Column("note", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("market", "ts", name="pk_tw_trading_calendar"),
    )

    op.create_table(
        "tw_broker_master",
        sa.Column("broker_id", sa.String(length=16), nullable=False),
        sa.Column("name_zh", sa.String(length=64), nullable=True),
        sa.Column("branch_name", sa.String(length=64), nullable=True),
        sa.Column("address", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("broker_id", name="pk_tw_broker_master"),
    )

    op.create_table(
        "tw_industry_chain",
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("industry", sa.String(length=64), nullable=False),
        sa.Column("sub_industry", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("symbol", "industry", name="pk_tw_industry_chain"),
    )
    op.create_index(
        "ix_tw_industry_chain_industry",
        "tw_industry_chain",
        ["industry"],
    )

    op.create_table(
        "tw_delisting",
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("delisted_at", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("symbol", name="pk_tw_delisting"),
    )


def downgrade() -> None:
    op.drop_table("tw_delisting")
    op.drop_index("ix_tw_industry_chain_industry", table_name="tw_industry_chain")
    op.drop_table("tw_industry_chain")
    op.drop_table("tw_broker_master")
    op.drop_table("tw_trading_calendar")
    op.drop_index("ix_tw_stock_info_industry", table_name="tw_stock_info")
    op.drop_table("tw_stock_info")
