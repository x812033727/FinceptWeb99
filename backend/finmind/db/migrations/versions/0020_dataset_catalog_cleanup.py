"""dataset_catalog cleanup: remove obsolete datasets + typo + News single_day

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-06

Three datasets in our `dataset_sources` are no longer present in
FinMind's `/api/v4/data` enum (verified 2026-05-06 by sending an invalid
dataset name and parsing the 422 enum-error response):

  - TaiwanStockBuyBack             — chain logged 422 Unprocessable Entity
  - TaiwanStockTradingDailyReport  — chain logged 422 Unprocessable Entity
  - TaiwanStockTradingDailyReportSecIdAgg — never had a row, but enum-rejected

Plus one typo: `TaiwanOptionTIck` (capital I) — FinMind's enum exposes
`TaiwanOptionTick` (lowercase). Our row will never match, the renamed
catalog entry seeds the correct name on next boot.

And one missing flag: `TaiwanStockNews` — verified 2026-05-06 the API
returns `400 "the dataset TaiwanStockNews size is too large, we only
send one day data, so end_date parameter need be none."` for any
multi-day request. It must be `single_day=true` in `dataset_sources`
so the scheduler emits per-day chunks; the corresponding catalog change
is in `dataset_catalog.py` (so future re-seeds carry the flag).

This migration:
  1. Deletes `backfill_progress` rows for the four obsolete/typo names
     (so future status reports don't carry stale "skipped" or 4xx-failed
     chunks for datasets that don't exist).
  2. Deletes `dataset_sources` rows for the four obsolete/typo names
     (init_db won't re-create them — they're gone from the catalog).
  3. Flips `dataset_sources.single_day = true` for TaiwanStockNews
     (init_db re-seeds idempotently from the catalog change, but
     existing rows preserve runtime state — including this flag — per
     `seed_dataset_sources`'s comment, so we set it here explicitly).
"""
from collections.abc import Sequence

from alembic import op


revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OBSOLETE = (
    "TaiwanStockBuyBack",
    "TaiwanStockTradingDailyReport",
    "TaiwanStockTradingDailyReportSecIdAgg",
    "TaiwanOptionTIck",
)


def upgrade() -> None:
    placeholders = ", ".join(f"'{c}'" for c in _OBSOLETE)
    op.execute(
        f"DELETE FROM backfill_progress WHERE dataset_code IN ({placeholders})"
    )
    op.execute(
        f"DELETE FROM dataset_sources  WHERE dataset_code IN ({placeholders})"
    )
    op.execute(
        "UPDATE dataset_sources SET single_day = true "
        "WHERE dataset_code = 'TaiwanStockNews'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE dataset_sources SET single_day = false "
        "WHERE dataset_code = 'TaiwanStockNews'"
    )
    # Don't recreate the obsolete dataset_sources rows on downgrade — the
    # catalog has been pruned in the same change, so a re-seed wouldn't
    # carry their metadata anyway. Re-running `init_db` after restoring
    # the catalog entries would re-insert them.
