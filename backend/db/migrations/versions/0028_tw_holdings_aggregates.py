"""tw_stock_shareholding + tw_market_institutional_daily

Revision ID: 0028
Revises: 0027
Create Date: 2026-05-01

Two TW chip-flavour archives in one migration since they share an
ingest cron + a discussion-context block:

  * `tw_stock_shareholding` — 股權分散表. Per (symbol, ts, bucket)
    long-form row: how shares are split across 15 holder-size
    brackets (1-999 shares = 散戶 ... 1000-lot+ = 大戶). Useful as
    a "散戶持股偏高 → 容易追殺殺" / "大戶集中度高 → 籌碼穩"
    signal. FinMind's response varies — sometimes one wide row per
    (symbol, ts) with bucket columns, sometimes per-bucket rows —
    we store long-form so re-shapes don't need a backfill.

  * `tw_market_institutional_daily` — 全市場三大法人. One row per
    trading day; aggregate buy/sell amounts across the entire
    market for foreign / SITC / dealer. Distinct from the existing
    per-symbol `TaiwanStockInstitutionalInvestors` archive in
    `tw_institutional_daily` (PR #131): that's per-stock granular
    flow, this is the index-level headline ("外資今日對台股淨買超
    +250 億").

Tables sized: shareholding ~1700 stocks × 15 buckets × weekly
publication = ~4 K rows/week ≈ 200 K rows/year (FinMind publishes
this weekly, not daily). market_institutional_daily ~250 rows/year
(one per trading day).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── 股權分散表 ───────────────────────────────────────────────
    op.create_table(
        "tw_stock_shareholding",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("bucket_id", sa.Integer(), nullable=False),
        sa.Column("bucket_label", sa.String(length=40), nullable=True),
        sa.Column("holders_count", sa.BigInteger(), nullable=True),
        sa.Column("shares_count", sa.BigInteger(), nullable=True),
        sa.Column("shares_percent", sa.Numeric(8, 4), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "ts", "bucket_id",
            name="pk_tw_stock_shareholding",
        ),
    )
    op.create_index(
        "ix_tw_stock_shareholding_lookup",
        "tw_stock_shareholding",
        ["market", "symbol", "ts"],
        unique=False,
    )

    # ── 全市場三大法人日報 ──────────────────────────────────────
    op.create_table(
        "tw_market_institutional_daily",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("foreign_buy", sa.BigInteger(), nullable=True),
        sa.Column("foreign_sell", sa.BigInteger(), nullable=True),
        sa.Column("sitc_buy", sa.BigInteger(), nullable=True),
        sa.Column("sitc_sell", sa.BigInteger(), nullable=True),
        sa.Column("dealer_buy", sa.BigInteger(), nullable=True),
        sa.Column("dealer_sell", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "ts", name="pk_tw_market_institutional_daily",
        ),
    )
    op.create_index(
        "ix_tw_market_institutional_daily_lookup",
        "tw_market_institutional_daily",
        ["market", "ts"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tw_market_institutional_daily_lookup",
        table_name="tw_market_institutional_daily",
    )
    op.drop_table("tw_market_institutional_daily")
    op.drop_index(
        "ix_tw_stock_shareholding_lookup",
        table_name="tw_stock_shareholding",
    )
    op.drop_table("tw_stock_shareholding")
