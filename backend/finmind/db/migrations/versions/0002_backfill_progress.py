"""backfill_progress — resumable Phase A backfill ledger

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-03

Tracks per-chunk progress for the FinMind sponsor-tier backfill so a
process restart can skip already-completed (dataset, symbol, range)
chunks instead of starting from scratch. See
`finmind/models/backfill_progress.py` for the full rationale.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "backfill_progress",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("dataset_code", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("range_start", sa.Date(), nullable=False),
        sa.Column("range_end", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("rows_written", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_backfill_progress"),
        sa.UniqueConstraint(
            "dataset_code",
            "symbol",
            "source",
            "range_start",
            "range_end",
            name="uq_backfill_progress_chunk",
        ),
    )
    op.create_index(
        "ix_backfill_progress_dataset_status",
        "backfill_progress",
        ["dataset_code", "status"],
        unique=False,
    )

    # Predicate index — Postgres-only feature. SQLite (used by tests)
    # silently ignores the predicate and creates a regular index, which
    # is also fine for test scale.
    if _is_postgres():
        op.execute(
            "CREATE INDEX ix_backfill_progress_resume "
            "ON backfill_progress (dataset_code, source, status) "
            "WHERE status IN ('pending', 'failed')"
        )
    else:
        op.create_index(
            "ix_backfill_progress_resume",
            "backfill_progress",
            ["dataset_code", "source", "status"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index(
        "ix_backfill_progress_resume", table_name="backfill_progress"
    )
    op.drop_index(
        "ix_backfill_progress_dataset_status",
        table_name="backfill_progress",
    )
    op.drop_table("backfill_progress")
