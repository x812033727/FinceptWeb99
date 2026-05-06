"""macro indicator tables: exchange rate, interest rate, US bond yield,
fear-greed, commodity price

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-06

Five new destination tables for FinMind macro datasets covered by the
canonical doc tutor pages but not previously wired up:

  - TaiwanExchangeRate           → tw_exchange_rate
  - InterestRate                 → macro_interest_rate
  - GovernmentBondsYield         → us_bond_yield     (FinMind only ships
                                                      US Treasury yields)
  - CnnFearGreedIndex            → us_fear_greed
  - CrudeOilPrices               → commodity_price   (also a slot for
                                                      future GoldPrice etc.)

All five are low-volume (one row per ((axis), date) so ~10y × 250 = 2,500
rows max per axis), no compression policy needed. Plain Postgres tables,
not timescaledb hypertables — the volume doesn't justify the chunk
overhead and the query pattern is "give me yesterday's USD rate" not
"scan a year of ticks".
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ingested_at() -> sa.Column:
    return sa.Column(
        "ingested_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


def upgrade() -> None:
    # ── TW central bank's daily exchange rate (cash + spot, buy + sell) ──
    op.create_table(
        "tw_exchange_rate",
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("cash_buy",  sa.Numeric(12, 4), nullable=True),
        sa.Column("cash_sell", sa.Numeric(12, 4), nullable=True),
        sa.Column("spot_buy",  sa.Numeric(12, 4), nullable=True),
        sa.Column("spot_sell", sa.Numeric(12, 4), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        _ingested_at(),
        sa.PrimaryKeyConstraint("currency", "ts", name="pk_tw_exchange_rate"),
    )

    # ── Multi-country central bank policy rates (FED/BOJ/ECB/...) ──
    op.create_table(
        "macro_interest_rate",
        sa.Column("country", sa.String(length=8), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("full_country_name", sa.String(length=64), nullable=True),
        sa.Column("interest_rate", sa.Numeric(8, 4), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        _ingested_at(),
        sa.PrimaryKeyConstraint("country", "ts", name="pk_macro_interest_rate"),
    )

    # ── US Treasury yield curve (1M..30Y) ──
    op.create_table(
        "us_bond_yield",
        # 'tenor' carries FinMind's full string ("United States 1-Month",
        # "United States 10-Year", ...) — we keep the verbose form so the
        # PK is stable if FinMind expands to other countries later.
        sa.Column("tenor", sa.String(length=32), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("yield_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        _ingested_at(),
        sa.PrimaryKeyConstraint("tenor", "ts", name="pk_us_bond_yield"),
    )

    # ── CNN US Fear & Greed Index (single time-series, 0..100) ──
    op.create_table(
        "us_fear_greed",
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("value", sa.SmallInteger(), nullable=True),
        sa.Column("emotion", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        _ingested_at(),
        sa.PrimaryKeyConstraint("ts", name="pk_us_fear_greed"),
    )

    # ── Commodity prices (currently Brent + WTI; room for Gold/Silver
    #    when those datasets are wired) ──
    op.create_table(
        "commodity_price",
        sa.Column("commodity", sa.String(length=16), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("price", sa.Numeric(12, 4), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        _ingested_at(),
        sa.PrimaryKeyConstraint("commodity", "ts", name="pk_commodity_price"),
    )


def downgrade() -> None:
    op.drop_table("commodity_price")
    op.drop_table("us_fear_greed")
    op.drop_table("us_bond_yield")
    op.drop_table("macro_interest_rate")
    op.drop_table("tw_exchange_rate")
