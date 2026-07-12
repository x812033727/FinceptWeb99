"""discussion_turns: add injected_by_user for B4 round-table interjections

Revision ID: 0069
Revises: 0068
Create Date: 2026-07-12

Adds a non-null boolean `injected_by_user` (server default false) to
`discussion_turns`. Marks turns that exist because the discussion owner
interjected:

  - the owner's own question turn (`persona_id="_user"`,
    `stance="user_input"` — between-rounds inject, mid-round interject
    and post-conclusion 追問 all produce one), and
  - the persona answer turn generated in direct response to a mid-round
    interjection / post-conclusion follow-up.

Ordinary persona turns from the round loop keep the default `false`, so
the frontend can render a distinguishing badge (使用者提問 / 插話回覆)
without guessing from content.

Note: `down_revision` points at 0068, owned by another in-flight branch —
the chain completes at merge time (coordinated numbering).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussion_turns",
        sa.Column(
            "injected_by_user",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("discussion_turns", "injected_by_user")
