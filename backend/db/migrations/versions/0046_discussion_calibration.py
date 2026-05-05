"""discussions — Brier score + outcome vector for calibration (PR-C1)

Revision ID: 0046
Revises: 0045
Create Date: 2026-05-05

PR-C1 closes the loop on PR-C0's per-symbol confidence by adding
the ground-truth-aware metric. Each post-window (D1-D5 fully
resolved) discussion gains:

  - `brier_score` — mean of (confidence - outcome_binary)² across
    every recommended symbol that resolved within the window.
    Lower is better; 0.25 is the chance baseline (uniform 0.5
    confidence). NULL when the window hasn't resolved yet OR the
    discussion was concluded before PR-C0 (no per-symbol
    confidence to score).

  - `outcome_vector` — per-symbol breakdown
    `[{symbol, confidence, outcome_binary}, ...]` so PR-C2's
    isotonic calibration fitter can pull (raw_conf, outcome) pairs
    across an entire strategy without re-deriving them.

The partial index `WHERE brier_score IS NOT NULL` keeps the sweep
aggregate query (which sums brier across resolved children) cheap
without bloating the full-table footprint — most rows are
in-flight or pre-PR-C0 and stay NULL.

This migration is additive: NULL on every existing row, so deploys
can roll backward without data loss.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussions",
        sa.Column("brier_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "discussions",
        sa.Column("outcome_vector", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_discussions_brier_score_resolved",
        "discussions",
        ["brier_score"],
        unique=False,
        postgresql_where=sa.text("brier_score IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_discussions_brier_score_resolved",
        table_name="discussions",
    )
    op.drop_column("discussions", "outcome_vector")
    op.drop_column("discussions", "brier_score")
