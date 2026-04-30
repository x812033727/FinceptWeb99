"""tw_revenue_monthly — TW monthly revenue archive

Revision ID: 0022
Revises: 0021
Create Date: 2026-04-30

Stores TW listed companies' monthly revenue (月營收) — the first
fundamental data point a public TW company releases each month
(deadline: 10th of the following month per TW securities law).
Captures the absolute revenue plus the upstream-reported
year-on-year and month-on-month growth percentages.

Schema choices mirror migration 0021 (chip metrics):
  - Composite PK (market, symbol, ts) where `ts` is the first day
    of the reporting month — idempotent UPSERT on monthly re-pull.
  - `revenue` is BIGINT in 千元 NTD (matches FinMind's unit), so a
    100 億 monthly revenue stays well under 64-bit range.
  - `revenue_yoy` / `revenue_mom` are Numeric(8,2) — typical values
    are -100 ~ +999 % so two decimals are plenty.

Plain table, not a hypertable: ~2000 rows/month × 12 months = 24K
rows/year. Negligible for plain Postgres.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tw_revenue_monthly",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("revenue", sa.BigInteger(), nullable=True),
        sa.Column("revenue_yoy", sa.Numeric(8, 2), nullable=True),
        sa.Column("revenue_mom", sa.Numeric(8, 2), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "ts", name="pk_tw_revenue_monthly",
        ),
    )
    op.create_index(
        "ix_tw_revenue_monthly_lookup",
        "tw_revenue_monthly",
        ["market", "symbol", "ts"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tw_revenue_monthly_lookup", table_name="tw_revenue_monthly",
    )
    op.drop_table("tw_revenue_monthly")
