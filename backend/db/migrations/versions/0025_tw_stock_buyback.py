"""tw_stock_buyback — TW listed-company buyback announcements

Revision ID: 0025
Revises: 0024
Create Date: 2026-05-01

庫藏股 (treasury-stock buyback) is the cleanest single-data-point
self-signal a TW listed company can send: management is willing to
spend cash to buy its own equity, almost always cited as "維護股東
權益 / 員工激勵". An announced buyback in the 1-3 days before /
during a price dip is the canonical buying-the-bottom pattern.

FinMind exposes this under the `TaiwanStockBuyBack` dataset
(sponsor-tier as of 2026-04 along with the rest of the corporate-
action feeds). One announcement per row; a single company can have
multiple active rounds so the PK is `(market, symbol, announce_date)`.
`current_buy_back_shares` updates day-by-day as the company executes,
so re-pulling the same `announce_date` row late in the period
overwrites with fresh execution progress.

Schema choices:
  - `max_shares` / `current_shares` are BigInteger (largest-cap TW
    companies could announce buybacks in the hundreds of millions
    of shares — comfortably under int64).
  - `price_lower` / `price_upper` are Numeric(12,4) — TW prices
    rarely exceed 4 decimals; covers everything from penny stocks
    to high-priced ICs (TSMC ~1000 NTD).
  - `method` is a small int (1, 2, 3 in the upstream feed) — store
    raw and let the read tier translate.
  - `purpose` is free Chinese text from the filing. 80 chars covers
    every variant we've seen ("維護股東權益及公司信用 / 轉讓予員工 …").

Plain table, not a hypertable: ~5-30 announcements per trading day
× 250 days = 7K rows/year. Negligible.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tw_stock_buyback",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("announce_date", sa.Date(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("method", sa.Integer(), nullable=True),
        sa.Column("purpose", sa.String(length=80), nullable=True),
        sa.Column("max_shares", sa.BigInteger(), nullable=True),
        sa.Column("current_shares", sa.BigInteger(), nullable=True),
        sa.Column("price_lower", sa.Numeric(12, 4), nullable=True),
        sa.Column("price_upper", sa.Numeric(12, 4), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "announce_date", name="pk_tw_stock_buyback",
        ),
    )
    # Composite index on (market, period_end, announce_date) lets the
    # discussion context aggregator efficiently filter "buybacks
    # currently in execution window" without scanning the whole table.
    op.create_index(
        "ix_tw_stock_buyback_active",
        "tw_stock_buyback",
        ["market", "period_end", "announce_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tw_stock_buyback_active", table_name="tw_stock_buyback",
    )
    op.drop_table("tw_stock_buyback")
