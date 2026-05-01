"""tw_govt_bank_flow_daily — 八大行庫每日買賣金額

Revision ID: 0026
Revises: 0025
Create Date: 2026-05-01

The "eight government banks" (八大行庫) — Bank of Taiwan, Land Bank,
Taiwan Cooperative Bank, First Commercial, Hua Nan, Chang Hwa, E.Sun
(state-share), Mega — collectively act as a state-aligned trading
desk in TW listed equities. Their net daily buy/sell is a quasi-
public-sector flow signal distinct from foreign / SITC / dealer
flows; analysts often cite "八大行庫今日進場 / 出場" as a
state-narrative input, especially during volatility windows.

FinMind's `TaiwanStockGovernmentBankBuySell` returns one row per
(date, bank_name) with daily buy + sell amounts in NTD. Sponsor-tier
as of 2026-04 along with the rest of the corporate-action feeds.

Schema:
  - Composite PK `(market, ts, bank_name)`. `ts` is a Date (one
    aggregate per trading day per bank, no intraday granularity).
  - `buy_amount` / `sell_amount` are BigInteger NTD — the
    aggregate eight-bank flow rarely exceeds tens of billions per
    day, comfortably under int64.

Plain table: 8 banks × 250 trading days = 2 000 rows/year.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tw_govt_bank_flow_daily",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("bank_name", sa.String(length=40), nullable=False),
        sa.Column("buy_amount", sa.BigInteger(), nullable=True),
        sa.Column("sell_amount", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "ts", "bank_name", name="pk_tw_govt_bank_flow_daily",
        ),
    )
    op.create_index(
        "ix_tw_govt_bank_flow_lookup",
        "tw_govt_bank_flow_daily",
        ["market", "ts"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tw_govt_bank_flow_lookup",
        table_name="tw_govt_bank_flow_daily",
    )
    op.drop_table("tw_govt_bank_flow_daily")
