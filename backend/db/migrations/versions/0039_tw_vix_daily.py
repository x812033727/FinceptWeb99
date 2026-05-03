"""tw_vix_daily — TAIFEX 臺指選擇權波動率指數 daily archive

Revision ID: 0039
Revises: 0038
Create Date: 2026-05-03

Single-scalar daily series (date → close) for the TAIWAN VIX
(臺指選擇權波動率指數). Source: TAIFEX
``https://www.taifex.com.tw/cht/3/vixDownload`` CSV download
(free + public, not in FinMind's catalogue).

Why a dedicated table for a single-column scalar:
  - Backtest mode anchors at ``as_of_date`` and needs the
    historical VIX value on that day. A 1-row-per-day archive
    is the simplest read tier.
  - The cron pulls a 30-day window every tick and upserts; the
    table never grows beyond ~250 rows/year × 1 market = trivial.

The discussion ctx block ``taiwan_vix`` reads the latest +
5-day-prior values and surfaces them with a change %, mirroring
the ``overseas_indicators`` ^VIX shape so personas can compare
TW vs US implied-volatility regimes side-by-side.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tw_vix_daily",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("vix_value", sa.Numeric(10, 4), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "ts", name="pk_tw_vix_daily",
        ),
    )
    op.create_index(
        "ix_tw_vix_daily_lookup",
        "tw_vix_daily",
        ["market", "ts"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tw_vix_daily_lookup", table_name="tw_vix_daily")
    op.drop_table("tw_vix_daily")
