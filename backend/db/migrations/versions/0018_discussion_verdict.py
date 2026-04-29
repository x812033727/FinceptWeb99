"""discussion verdict columns — auto-run + self-grading

Revision ID: 0018
Revises: 0017
Create Date: 2026-04-29

Extends `discussions` so each round-table session can carry its own
"prediction with feedback" trail:

  - `auto_run` flags rows produced by the daily scheduler (vs user-
    created manually). Default false so existing rows aren't rewritten.
  - `recommended_symbols` already lives in the `conclusion` JSON; we
    snapshot the day-1 open price for each one in `day1_open_prices`
    so the verdict math doesn't depend on which intraday price source
    is responding when the verifier runs.
  - `verify_after_date` is set when the synthesizer finishes — the
    verifier task scans daily and only grades rows whose window has
    closed.
  - `verdict` ∈ {win, loss, unverifiable, NULL}. NULL = pending.
  - `verdict_reason` carries a one-line explanation for the admin UI.

A partial index on (verify_after_date) WHERE auto_run=true AND
verdict IS NULL keeps the verifier's nightly scan cheap once history
accumulates. Postgres-only — SQLite (test env) silently builds a
regular index instead, which is fine for the test suite's sizes.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.add_column(
        "discussions",
        sa.Column("verdict", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "discussions",
        sa.Column("verdict_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "discussions",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "discussions",
        sa.Column(
            "auto_run",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "discussions",
        sa.Column("day1_open_prices", sa.JSON(), nullable=True),
    )
    op.add_column(
        "discussions",
        sa.Column("verify_after_date", sa.Date(), nullable=True),
    )

    # Partial index covers exactly what the verifier scans nightly:
    # auto_run rows whose verdict is still pending and whose 5-day
    # window has expired. Skip the partial predicate on SQLite (it
    # ignores it silently but `postgresql_where` raises on some
    # alembic versions when the dialect doesn't support it).
    if _is_postgres():
        op.create_index(
            "ix_discussions_verify_pending",
            "discussions",
            ["verify_after_date"],
            postgresql_where=sa.text("auto_run = true AND verdict IS NULL"),
        )
    else:
        op.create_index(
            "ix_discussions_verify_pending",
            "discussions",
            ["verify_after_date"],
        )


def downgrade() -> None:
    op.drop_index("ix_discussions_verify_pending", table_name="discussions")
    op.drop_column("discussions", "verify_after_date")
    op.drop_column("discussions", "day1_open_prices")
    op.drop_column("discussions", "auto_run")
    op.drop_column("discussions", "verified_at")
    op.drop_column("discussions", "verdict_reason")
    op.drop_column("discussions", "verdict")
