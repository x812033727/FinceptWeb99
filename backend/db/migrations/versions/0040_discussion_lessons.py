"""discussion_lessons — structured post-mortem takeaways

Revision ID: 0040
Revises: 0039
Create Date: 2026-05-05

Each post-mortem (verdict=miss) synthesizer pass extracts a small
list of structured lessons. Subsequent discussions on the same
market read them back via `gather_market_context` so personas see
"what past discussions on this market got wrong" alongside the
live ctx.

Owner-scoped: `owner_user_id` matches the discussion's owner. A
multi-admin shared deployment doesn't bleed one operator's
lessons into another's prompt.

Backtest correctness: the read tier filters by `as_of_date <=
discussion.as_of_date` so a sweep discussion anchored at
2025-06-01 never sees lessons learned from 2025-06-15.

`related_symbols` / `missed_winners` are JSON arrays rather than a
PostgreSQL native `text[]` so the same migration runs in tests
against the in-memory SQLite engine. Per-symbol matching is done in
Python after the owner+market btree filter narrows the candidate
set to ~100 rows for a typical year of usage — a GIN index buys
nothing at that scale.

The `lesson_text_hash` index supports the soft dedup at write time
— if the same lesson_text was persisted within the dedup window,
the duplicate is dropped silently.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discussion_lessons",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "discussion_id",
            UUID(as_uuid=True),
            sa.ForeignKey("discussions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("market", sa.String(length=16), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("lesson_text", sa.Text(), nullable=False),
        sa.Column(
            "lesson_text_hash", sa.String(length=64), nullable=False,
        ),
        sa.Column(
            "related_symbols", sa.JSON(),
            nullable=False, server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "missed_winners", sa.JSON(),
            nullable=False, server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
    )
    op.create_index(
        "ix_discussion_lessons_owner_market_asof",
        "discussion_lessons",
        ["owner_user_id", "market", "as_of_date"],
        unique=False,
    )
    op.create_index(
        "ix_discussion_lessons_hash_recent",
        "discussion_lessons",
        ["owner_user_id", "lesson_text_hash", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_discussion_lessons_hash_recent",
        table_name="discussion_lessons",
    )
    op.drop_index(
        "ix_discussion_lessons_owner_market_asof",
        table_name="discussion_lessons",
    )
    op.drop_table("discussion_lessons")
