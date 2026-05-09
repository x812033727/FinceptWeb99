"""ingest_health_history — append-only outcome log for sparkline UI

Revision ID: 0057
Revises: 0056
Create Date: 2026-05-09

The existing `record_health` writes one Redis blob per job (TTL 7d)
that the AdminPage IngestHealthCard reads as a single "current row"
snapshot. That's enough to show "is this cron healthy NOW", but
gives no signal on whether a job that's currently red has been red
for 1 hour or 30 days — the dashboard can't distinguish a freshly-
broken cron from a long-term zombie.

This table archives every record_health call as a tiny row
(`job_id, recorded_at, outcome, row_count`) so the dashboard can
render a 7-day sparkline per job. Outcome labels mirror the Prometheus
counter `ingest_runs_total{outcome}` exactly so the sparkline aggregate
and the metric stay in lockstep:

  ok | silent_deny | failed | skipped

Storage: at ~50 jobs × 24 runs/day average, that's ~36k rows/month,
~10 MB at 100 bytes/row. Negligible. A daily prune cron can be added
later if real-world growth ever justifies it; for now operators who
want manual retention can run:

    DELETE FROM ingest_health_history
    WHERE recorded_at < NOW() - INTERVAL '60 days';

The composite index `(job_id, recorded_at DESC)` matches the only
query the UI runs — "give me the last N days of outcomes for this
job" — so the sparkline lookup is a single index range scan.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingest_health_history",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        # Mirrors `_classify_outcome` in services/ingest/repository.py.
        # If a new label is added there, this column gains a value
        # silently — no constraint check, no migration needed.
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column(
            "row_count", sa.Integer(), nullable=False, server_default="0",
        ),
    )
    op.create_index(
        "ix_ingest_health_history_lookup",
        "ingest_health_history",
        ["job_id", "recorded_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ingest_health_history_lookup",
        table_name="ingest_health_history",
    )
    op.drop_table("ingest_health_history")
