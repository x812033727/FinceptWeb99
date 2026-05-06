"""dataset_catalog cleanup: disable FinMind-removed datasets, fix typo,
flag News as single_day

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-06

Three datasets in our `dataset_sources` are no longer present in
FinMind's `/api/v4/data` enum (verified 2026-05-06 by sending an invalid
dataset name and parsing the resulting 422 enum-error):

  - TaiwanStockBuyBack             — chain logged 422 Unprocessable Entity
  - TaiwanStockTradingDailyReport  — chain logged 422 Unprocessable Entity
  - TaiwanStockTradingDailyReportSecIdAgg — never had a row, but enum-rejected

These remain in `data/tw/finmind_datasets.py` and `dataset_catalog.py`
because the registry/catalog invariants are bidirectional, and the
connector still references the first two (`get_buyback_market_wide`,
`get_broker_concentration_per_symbol`). Disabling them via
`dataset_sources.enabled=false` keeps the scheduler / chain from
trying them while leaving the spec/catalog wiring intact.

Plus one typo: `TaiwanOptionTIck` (capital I) — FinMind's enum exposes
`TaiwanOptionTick` (lowercase). Our row will never match. Migration
deletes the typo'd row; the renamed catalog entry seeds the corrected
name on next boot.

And one missing flag: `TaiwanStockNews` — verified 2026-05-06 the API
returns `400 "the dataset TaiwanStockNews size is too large, we only
send one day data, so end_date parameter need be none."` for any
multi-day request. Must be `single_day=true` so the scheduler emits
per-day chunks; the corresponding catalog change is in
`dataset_catalog.py` so future re-seeds carry the flag.

Migration:
  1. UPDATE dataset_sources.enabled=false for the three FinMind-removed
     datasets. init_db's UPSERT preserves `enabled` per its design
     comment, so the disabled state survives re-seeds.
  2. DELETE the typo'd `TaiwanOptionTIck` row from dataset_sources and
     its backfill_progress chunks. Init_db re-creates the row under
     the corrected `TaiwanOptionTick` name on next boot.
  3. UPDATE dataset_sources.single_day=true for TaiwanStockNews
     (init_db preserves `single_day` too, so this also persists).
  4. DELETE backfill_progress rows for the four obsolete / typo names
     so future status reports don't carry stale "skipped" or
     422-failed chunks for datasets that don't exist or got renamed.
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
)


def upgrade() -> None:
    obsolete_list = ", ".join(f"'{c}'" for c in _OBSOLETE)
    typo_and_obsolete = obsolete_list + ", 'TaiwanOptionTIck'"

    # 1. Disable the three FinMind-removed datasets so the scheduler /
    #    chain stops trying them.
    op.execute(
        f"UPDATE dataset_sources SET enabled = false "
        f"WHERE dataset_code IN ({obsolete_list})"
    )

    # 2. Delete the typo'd row — init_db re-seeds the corrected name.
    op.execute(
        "DELETE FROM dataset_sources WHERE dataset_code = 'TaiwanOptionTIck'"
    )

    # 3. Mark News as single_day per its API contract.
    op.execute(
        "UPDATE dataset_sources SET single_day = true "
        "WHERE dataset_code = 'TaiwanStockNews'"
    )

    # 4. Clean stale backfill_progress for the four obsolete / typo names
    #    so future status reports don't carry the dead 422-failed and
    #    "no ingest mapping" rows.
    op.execute(
        f"DELETE FROM backfill_progress WHERE dataset_code IN ({typo_and_obsolete})"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE dataset_sources SET single_day = false "
        "WHERE dataset_code = 'TaiwanStockNews'"
    )
    obsolete_list = ", ".join(f"'{c}'" for c in _OBSOLETE)
    op.execute(
        f"UPDATE dataset_sources SET enabled = true "
        f"WHERE dataset_code IN ({obsolete_list})"
    )
    # The typo'd row stays dropped on downgrade — restoring the catalog
    # to use 'TaiwanOptionTIck' is a separate code change.
