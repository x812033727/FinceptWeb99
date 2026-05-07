"""discussions — post_mortem_diff for original-vs-revised comparison (PR-2)

Revision ID: 0050
Revises: 0049
Create Date: 2026-05-07

PR #272 split the post-mortem revised conclusion into its own
`post_mortem_conclusion` JSONB column so the original `conclusion`
stays intact for side-by-side comparison. The UI renders both cards
but the human still has to eyeball "what changed" — added picks,
dropped picks, confidence shifts, reasoning differences.

This migration adds a `post_mortem_diff` JSONB column populated by
`synthesize_conclusion` whenever it writes a `post_mortem_conclusion`,
holding the structured delta:

    {
      "symbols_added":   ["..."],
      "symbols_removed": ["..."],
      "confidence_changes": {"<symbol>": {"orig": 0.7, "post": 0.5}},
      "consensus_score_delta": -0.05,
      "time_horizon_changed": false,
      "reasoning_overlap": 0.42  // jaccard over tokens
    }

Existing rows with post-mortem conclusions stay NULL (no automatic
backfill — the diff can be recomputed on demand from the persisted
pair when needed).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussions",
        sa.Column(
            "post_mortem_diff",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(
                sa.JSON(), "sqlite",
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("discussions", "post_mortem_diff")
