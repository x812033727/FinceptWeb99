"""add data quality feedback for beta operations

Revision ID: 0079
Revises: 0078
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0079"
down_revision: str | None = "0078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "data_quality_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("market", sa.String(12), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=True),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.String(300), nullable=True),
        sa.Column("observed_meta", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_data_quality_feedback_status_created", "data_quality_feedback", ["status", "created_at"])
    op.create_index("ix_data_quality_feedback_user_id", "data_quality_feedback", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_data_quality_feedback_user_id", table_name="data_quality_feedback")
    op.drop_index("ix_data_quality_feedback_status_created", table_name="data_quality_feedback")
    op.drop_table("data_quality_feedback")
