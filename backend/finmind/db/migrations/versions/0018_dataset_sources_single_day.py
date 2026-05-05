"""dataset_sources: add `single_day` flag and seed known values

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-05

`single_day=true` marks datasets that FinMind only serves one date
per call (KBar, PriceTick, BlockTradingDailyReport, GovernmentBank-
BuySell, TradingDailyReport, WarrantTradingDailyReport — multi-day
queries error with "size is too large, end_date parameter need be
none"). The mapping layer already had a `single_day` flag for the
runner's day-by-day fan-out; mirroring it on `dataset_sources` lets
the **scheduler** (`expand_due_datasets`) emit one DueChunk per day
instead of a multi-day chunk that the runner would internally split.

Why move it to the catalog level:
  - Per-day chunks in `backfill_progress` mean retry granularity is
    per-day. A failed day re-fetches just that day instead of the
    whole range when `claim_chunk` re-claims.
  - Operators can flip `single_day` per-dataset via SQL / admin UI
    without a code deploy if FinMind changes their endpoint shape.
  - Status / coverage reports can show "X single-day datasets, Y
    expected days/window" with no extra plumbing.

The mapping-level flag stays as the runner backstop for two callers
the scheduler doesn't reach: `backfill --dataset X --days N` and
direct `ingest_chunk(...)` calls. If a multi-day chunk arrives for a
single_day dataset (e.g. from those CLI paths), the runner still
iterates day-by-day per the mapping flag.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SINGLE_DAY_DATASETS = (
    "TaiwanStockKBar",
    "TaiwanStockPriceTick",
    "TaiwanStockBlockTradingDailyReport",
    "TaiwanStockGovernmentBankBuySell",
    "TaiwanStockTradingDailyReport",
    "TaiwanStockWarrantTradingDailyReport",
)


def upgrade() -> None:
    op.add_column(
        "dataset_sources",
        sa.Column(
            "single_day",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Drop the temporary default — future inserts must come from the
    # seed (init_db.seed_dataset_sources), which sets the flag from
    # the catalog. Leaving the default would mean a stray INSERT could
    # silently land with the wrong shape.
    op.alter_column("dataset_sources", "single_day", server_default=None)

    # Seed the known single-day datasets. Runs idempotently — already-
    # correct rows match the SET and stay unchanged.
    placeholders = ", ".join(f"'{c}'" for c in _SINGLE_DAY_DATASETS)
    op.execute(
        f"UPDATE dataset_sources SET single_day=true "
        f"WHERE dataset_code IN ({placeholders})"
    )


def downgrade() -> None:
    op.drop_column("dataset_sources", "single_day")
