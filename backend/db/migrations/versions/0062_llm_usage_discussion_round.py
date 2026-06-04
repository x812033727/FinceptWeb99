"""llm_usage_events: add discussion_id + round for per-round token tally

Revision ID: 0062
Revises: 0061
Create Date: 2026-06-04

Adds discussion attribution to `llm_usage_events` so token usage can be
summed per discussion round (the "每輪結算給 AI 的 token" feature). Both
columns are nullable: non-discussion callers (news-sentiment scorer,
direct chat) and every pre-existing row stay valid with NULL. `round` is
NULL for the synthesizer/conclusion call, which is not part of any single
round. An index on (discussion_id, round) backs the per-round aggregation
query and the GET /sessions/{id}/round-usage endpoint.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_usage_events",
        sa.Column("discussion_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "llm_usage_events",
        sa.Column("round", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_llm_usage_events_discussion_id_discussions",
        "llm_usage_events",
        "discussions",
        ["discussion_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_llm_usage_discussion_round",
        "llm_usage_events",
        ["discussion_id", "round"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_usage_discussion_round", table_name="llm_usage_events")
    op.drop_constraint(
        "fk_llm_usage_events_discussion_id_discussions",
        "llm_usage_events",
        type_="foreignkey",
    )
    op.drop_column("llm_usage_events", "round")
    op.drop_column("llm_usage_events", "discussion_id")
