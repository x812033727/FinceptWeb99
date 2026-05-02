"""discussion_turns: UNIQUE(discussion_id, round, turn_index)

Revision ID: 0030
Revises: 0029
Create Date: 2026-05-02

`DiscussionTurn` had only a non-unique lookup index on the trio. The
service-layer `inject_user_message` computes `turn_index = max + 1`
from a SELECT and then INSERTs, so two concurrent injections on the
same round both saw `max=N` and produced two rows with
`turn_index=N+1` — display order ambiguous, no constraint failure.

This migration upgrades the existing index to a UNIQUE INDEX so the
race surfaces as an `IntegrityError` instead of silent duplicates.
The service catches and retries with the freshly-bumped max.

The lookup index is preserved (Postgres + SQLite both allow a UNIQUE
INDEX to serve as a covering index for the same column triple).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop the existing non-unique lookup index, replace with a unique
    # one over the same column triple. Postgres' `IF EXISTS` guard
    # makes the migration idempotent if a previous attempt half-
    # finished. SQLite supports the same syntax.
    op.drop_index(
        "ix_discussion_turns_lookup",
        table_name="discussion_turns",
        if_exists=True,
    )
    op.create_index(
        "ix_discussion_turns_lookup",
        "discussion_turns",
        ["discussion_id", "round", "turn_index"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_discussion_turns_lookup",
        table_name="discussion_turns",
        if_exists=True,
    )
    op.create_index(
        "ix_discussion_turns_lookup",
        "discussion_turns",
        ["discussion_id", "round", "turn_index"],
        unique=False,
    )
