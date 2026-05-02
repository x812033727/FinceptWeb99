"""Backfill verify_after_date for legacy concluded manual discussions.

Revision ID: 0032
Revises: 0031
Create Date: 2026-05-02

PR #218 made `synthesize_conclusion` seed `verify_after_date` so the
verifier could grade manual (non-auto-run) discussions. Going
forward every concluded discussion gets the field populated.

But existing concluded manual discussions in production already have
`verdict=NULL, verify_after_date=NULL` and never will be picked up by
the verifier — `prior_discussions.verdict` for those rows stays
`null` indefinitely, defeating the cross-session memory feature for
historical data.

This migration backfills `verify_after_date` for those rows so the
next verifier cron (08:30 UTC daily) sweeps them in. We use
`created_at + 7 calendar days` as a coarse proxy for "5 trading
days" — for old rows this is already in the past, so the verifier
processes them on the next tick. The verifier's date-comparison
gate (`verify_after_date <= today`) is the only place this date
matters, so coarse precision is fine.

Auto-run rows are already populated (the auto-run cron sets it)
and aren't touched by this UPDATE.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Postgres + SQLite both support the SQL-standard CAST + INTERVAL,
    # but SQLite's syntax differs slightly. The dialect-detection
    # branch keeps the same intent ("created_at + 7 days") portable.
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            "UPDATE discussions "
            "SET verify_after_date = date(created_at, '+7 days') "
            "WHERE status = 'done' "
            "  AND verdict IS NULL "
            "  AND verify_after_date IS NULL"
        )
    else:
        op.execute(
            "UPDATE discussions "
            "SET verify_after_date = (created_at::date + INTERVAL '7 days')::date "
            "WHERE status = 'done' "
            "  AND verdict IS NULL "
            "  AND verify_after_date IS NULL"
        )


def downgrade() -> None:
    # The backfill is idempotent in spirit — there's no exact
    # "before" state to roll back to (we don't know which rows we
    # set vs. which were always populated). Downgrade nulls out
    # rows that look like they were touched (status=done,
    # verdict=null, verify_after_date populated) — best-effort, may
    # over-clear in some edge cases. Safer than no downgrade path.
    op.execute(
        "UPDATE discussions "
        "SET verify_after_date = NULL "
        "WHERE status = 'done' "
        "  AND verdict IS NULL "
        "  AND verify_after_date IS NOT NULL"
    )
