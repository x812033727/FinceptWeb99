"""discussions.post_mortem_conclusion — preserve original conclusion

Revision ID: 0035
Revises: 0034
Create Date: 2026-05-03

Before this PR, the post-mortem self-critique flow (PR #249) ran
`/post-mortem` (inject) → `/round` (reflect) → `/conclude`
(re-synthesize), and the re-synthesize step **overwrote** the
original `conclusion` JSON. Operators lost the ability to compare
the analyst's first-pass take vs the post-critique revision —
the most useful diagnostic in the whole flow.

New nullable JSONB column to receive the post-mortem-driven
conclusion. The synthesizer (PR #272 same SHA) routes its write
based on whether the transcript contains a `_user/user_input` turn
whose content starts with "【事後檢討":
  - has_post_mortem == True  → write to `post_mortem_conclusion`
  - has_post_mortem == False → write to `conclusion` (existing path)

Backward compat: NULL means "no post-mortem has run". Existing rows
keep `conclusion` intact; the UI reads the new column and renders
a second card alongside the original when populated.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussions",
        sa.Column("post_mortem_conclusion", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("discussions", "post_mortem_conclusion")
