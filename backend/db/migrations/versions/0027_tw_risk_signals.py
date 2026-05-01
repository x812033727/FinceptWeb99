"""tw_risk_signals — three TW risk-warning archives in one migration

Revision ID: 0027
Revises: 0026
Create Date: 2026-05-01

Three small tables sharing a single migration because they're
ingested + read together for the discussion-context "risk warnings"
block:

  - `tw_stock_disposition`: 處置股票 — TWSE / TPEx places a stock
    under disposition (Level 1 / 2 / 3) when 5-day volatility
    breaches thresholds. Trading restrictions kick in (cash-only
    settlement, lower lot caps, no margin). The window has explicit
    `period_start` / `period_end` so the discussion-context filter
    "currently in disposition" is just `period_end >= today`.
  - `tw_stock_suspended`: 暫停交易 — sparse event log for full
    trading halts. Tiny table; one row per (symbol, date).
  - `tw_stock_day_trading_daily`: 現股當沖 daily volume
    aggregates. One row per (symbol, date). Stores raw `volume`
    + `buy_amount` + `sell_amount` so the ratio computation lives
    at read time (lets us re-tune the "high day-trading ratio"
    threshold without a backfill).

Sponsor-tier datasets as of 2026-04. All three use the shared
FinMind paywall fail-soft pattern from PR #188.

Schema sizing: disposition + suspended are sparse event-log tables
(< 1000 rows total). day_trading_daily writes ~1700 rows/day × 30
day rolling lookback = 50K active rows; full-history archive over
years would hit millions, so we deliberately cap the cron's
lookback at 30 days and let older rows naturally drop off via a
companion retention prune (TODO: add to ingest_quotes_retention
later if the table grows uncomfortably).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Disposition (處置股) ──────────────────────────────────────
    op.create_table(
        "tw_stock_disposition",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("classification", sa.String(length=40), nullable=True),
        sa.Column("level", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "period_start",
            name="pk_tw_stock_disposition",
        ),
    )
    op.create_index(
        "ix_tw_stock_disposition_active",
        "tw_stock_disposition",
        ["market", "period_end"],
        unique=False,
    )

    # ── Suspended (暫停交易) ──────────────────────────────────────
    op.create_table(
        "tw_stock_suspended",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=True),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "ts", name="pk_tw_stock_suspended",
        ),
    )
    op.create_index(
        "ix_tw_stock_suspended_recent",
        "tw_stock_suspended",
        ["market", "ts"],
        unique=False,
    )

    # ── Day-trading daily volume (現股當沖) ──────────────────────
    op.create_table(
        "tw_stock_day_trading_daily",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("buy_amount", sa.BigInteger(), nullable=True),
        sa.Column("sell_amount", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "ts",
            name="pk_tw_stock_day_trading_daily",
        ),
    )
    op.create_index(
        "ix_tw_stock_day_trading_lookup",
        "tw_stock_day_trading_daily",
        ["market", "ts"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tw_stock_day_trading_lookup",
        table_name="tw_stock_day_trading_daily",
    )
    op.drop_table("tw_stock_day_trading_daily")
    op.drop_index(
        "ix_tw_stock_suspended_recent",
        table_name="tw_stock_suspended",
    )
    op.drop_table("tw_stock_suspended")
    op.drop_index(
        "ix_tw_stock_disposition_active",
        table_name="tw_stock_disposition",
    )
    op.drop_table("tw_stock_disposition")
