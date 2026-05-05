"""backfill_progress unique key — NULLS NOT DISTINCT for market-wide chunks

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-05

Postgres treats NULLs as distinct in unique constraints by default
(SQL standard). Market-wide datasets (TaiwanStockInfo,
TaiwanBusinessIndicator, …) record their chunks with `symbol IS NULL`,
so the original `UNIQUE (dataset_code, symbol, source, range_start,
range_end)` constraint accepted unlimited duplicate rows for the same
market-wide chunk.

Symptom: `claim_chunk()` does a Postgres upsert via
`ON CONFLICT (dataset_code, symbol, source, range_start, range_end)
 DO UPDATE`, then re-selects the row with `.scalar_one()`. Because the
ON CONFLICT couldn't match NULL-symbol rows, every retry inserted a
fresh duplicate, and the post-upsert SELECT eventually raised
`MultipleResultsFound`. Observed live: TaiwanBusinessIndicator
accumulated 15 rows for the same range, blocking every cron tick.

Fix: drop the constraint, dedupe existing rows (keep MAX(id) per
chunk-tuple — preserves the most recent run's status/rows_written),
re-add with `NULLS NOT DISTINCT` (PG15+; we run 15.17). Now NULL
symbol behaves like a value in the index, the upsert matches, and
`claim_chunk` stays single-row-correct.

SQLite path (tests) is a no-op — SQLite already treats NULLs as equal
in UNIQUE indexes, which is why the test suite never hit this.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    # Dedupe before re-adding the constraint, otherwise the ALTER fails.
    # `id IS DISTINCT FROM (latest)` keeps exactly one row per chunk-tuple
    # (the highest id — most recent run). `IS NOT DISTINCT FROM` makes
    # NULL symbol group with NULL symbol.
    op.execute(
        """
        DELETE FROM backfill_progress AS bp
        WHERE bp.id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY dataset_code, symbol, source,
                                 range_start, range_end
                    ORDER BY id DESC
                ) AS rn
                FROM backfill_progress
            ) ranked
            WHERE ranked.rn > 1
        )
        """
    )
    op.execute(
        "ALTER TABLE backfill_progress "
        "DROP CONSTRAINT uq_backfill_progress_chunk"
    )
    op.execute(
        "ALTER TABLE backfill_progress "
        "ADD CONSTRAINT uq_backfill_progress_chunk "
        "UNIQUE NULLS NOT DISTINCT "
        "(dataset_code, symbol, source, range_start, range_end)"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        "ALTER TABLE backfill_progress "
        "DROP CONSTRAINT uq_backfill_progress_chunk"
    )
    op.execute(
        "ALTER TABLE backfill_progress "
        "ADD CONSTRAINT uq_backfill_progress_chunk "
        "UNIQUE (dataset_code, symbol, source, range_start, range_end)"
    )
