"""fundamentals_snapshots — daily PE / PB / dividend_yield archive

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-28

Phase 3 of the scheduled-fetch subsystem. Stores one row per
(symbol, as_of) capturing the day's valuation ratios. A daily cron
calls TWSE BWIBBU_ALL — one HTTP request returns the full cross-section
for every TWSE-listed security, no quota — so the table tracks
historical PE / PB / dividend yield without needing per-symbol fetches.

Schema choices:
  - Composite PK (market, symbol, as_of) so daily ingest is an idempotent
    UPSERT (re-running on the same day overwrites in place).
  - `eps`, `revenue` are nullable — TWSE BWIBBU doesn't expose them; a
    future FinMind enrichment task can fill them per shard, quota
    permitting. `payload` jsonb captures any source-specific extras
    (e.g. dividend_year) without growing the column count.
  - Plain table, not a hypertable: ~2000 rows/day × 365 = ~730K/year is
    too small to justify TimescaleDB chunking.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fundamentals_snapshots",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("pe_ratio", sa.Numeric(12, 4), nullable=True),
        sa.Column("pb_ratio", sa.Numeric(12, 4), nullable=True),
        sa.Column("dividend_yield", sa.Numeric(8, 4), nullable=True),
        sa.Column("eps", sa.Numeric(18, 6), nullable=True),
        sa.Column("revenue", sa.Numeric(20, 2), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "as_of", name="pk_fundamentals_snapshots",
        ),
    )
    op.create_index(
        "ix_fundamentals_snapshots_lookup",
        "fundamentals_snapshots",
        ["market", "symbol", "as_of"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fundamentals_snapshots_lookup", table_name="fundamentals_snapshots",
    )
    op.drop_table("fundamentals_snapshots")
