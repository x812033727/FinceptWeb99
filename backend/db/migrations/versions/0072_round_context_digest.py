"""discussion_round_contexts: add nullable `digest` column

Revision ID: 0072
Revises: 0071
Create Date: 2026-07-13

R6 PR2 round digest: stores a compact ~300-word recap of each round's
debate alongside the existing per-round context snapshot. Nullable —
populated only when `DISCUSSION_ROUND_DIGEST_ENABLED` is on (off by
default), so existing rows and every round run with the feature disabled
simply carry NULL. Additive and behaviour-neutral for the discussion
flow itself.

Idempotent: `ADD COLUMN IF NOT EXISTS` so it applies cleanly whether or
not the column was already created out-of-band.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE discussion_round_contexts "
        "ADD COLUMN IF NOT EXISTS digest TEXT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE discussion_round_contexts DROP COLUMN IF EXISTS digest"
    )
