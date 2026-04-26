"""add llm_usage_events table

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_usage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("persona_id", sa.String(64), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_llm_usage_user_time", "llm_usage_events", ["user_id", "created_at"])
    op.create_index("ix_llm_usage_provider_time", "llm_usage_events", ["provider", "created_at"])
    op.create_index("ix_llm_usage_events_user_id", "llm_usage_events", ["user_id"])
    op.create_index("ix_llm_usage_events_provider", "llm_usage_events", ["provider"])
    op.create_index("ix_llm_usage_events_created_at", "llm_usage_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_events_created_at", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_events_provider", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_events_user_id", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_provider_time", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_user_time", table_name="llm_usage_events")
    op.drop_table("llm_usage_events")
