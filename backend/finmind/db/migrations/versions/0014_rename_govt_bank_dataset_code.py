"""rename `TaiwanstockGovernmentBankBuySell` → `TaiwanStockGovernmentBankBuySell`

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-05

The catalog had a casing typo (`Taiwan**s**tock…` — lowercase `s`)
that didn't match FinMind's actual dataset code (`Taiwan**S**tock…`).
The connector layer hardcodes the correct casing, but the runner
queries FinMind via the catalog's code, so this dataset always 422'd
at FinMind's endpoint validator before any tier/quota check.

The catalog source files (`data/tw/finmind_datasets.py`,
`finmind/dataset_catalog.py`) are fixed in the same commit; this
migration cleans up the row in `dataset_sources` so existing deploys
pick up the new code on the next `init_db` seed without leaving the
old code orphaned alongside the new one (the seed UPSERTs by
`dataset_code` — without this rename the old typo'd row would
linger as a phantom enabled-but-broken entry).

Idempotent: if the typo'd row was never seeded (fresh DB) the UPDATE
hits 0 rows and the migration succeeds. If both the old and new
rows exist (operator manually inserted), keep the new one and drop
the old.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD = "TaiwanstockGovernmentBankBuySell"
_NEW = "TaiwanStockGovernmentBankBuySell"


def upgrade() -> None:
    # If both old and new exist (rare — only when an operator manually
    # inserted the new code while the old one lingered), drop the old.
    op.execute(
        f"""
        DELETE FROM dataset_sources
         WHERE dataset_code = '{_OLD}'
           AND EXISTS (
               SELECT 1 FROM dataset_sources WHERE dataset_code = '{_NEW}'
           )
        """
    )
    # Otherwise rename in place — preserves any per-row state operators
    # may have set (enabled flag, last_ingest_at telemetry, manually
    # flipped active_source for fallback testing, etc.).
    op.execute(
        f"UPDATE dataset_sources SET dataset_code = '{_NEW}' "
        f"WHERE dataset_code = '{_OLD}'"
    )

    # The same dataset_code may appear in `backfill_progress` from
    # earlier ingestion attempts. Rename those ledger rows too so the
    # admin-page view groups them correctly under the new code.
    op.execute(
        f"UPDATE backfill_progress SET dataset_code = '{_NEW}' "
        f"WHERE dataset_code = '{_OLD}'"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE dataset_sources SET dataset_code = '{_OLD}' "
        f"WHERE dataset_code = '{_NEW}'"
    )
    op.execute(
        f"UPDATE backfill_progress SET dataset_code = '{_OLD}' "
        f"WHERE dataset_code = '{_NEW}'"
    )
