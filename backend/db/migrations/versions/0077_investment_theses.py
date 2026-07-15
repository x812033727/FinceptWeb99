"""add owner-scoped investment theses and event timeline

Revision ID: 0077
Revises: 0076
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0077"
down_revision: str | None = "0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "investment_theses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("market", sa.String(8), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("core_case", sa.Text(), nullable=False),
        sa.Column("catalysts", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("risks", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("valuation", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("watch_conditions", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("review_date", sa.Date(), nullable=True),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_investment_theses_user_updated", "investment_theses", ["user_id", "updated_at"])
    op.create_table(
        "thesis_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("thesis_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("investment_theses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("source", sa.String(80), nullable=True),
        sa.Column("source_ref", sa.String(500), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("thesis_id", "event_type", "source_ref", name="uq_thesis_event_source"),
    )
    op.create_index("ix_thesis_events_thesis_occurred", "thesis_events", ["thesis_id", "occurred_at"])
    op.create_index("ix_thesis_events_user_id", "thesis_events", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_thesis_events_user_id", table_name="thesis_events")
    op.drop_index("ix_thesis_events_thesis_occurred", table_name="thesis_events")
    op.drop_table("thesis_events")
    op.drop_index("ix_investment_theses_user_updated", table_name="investment_theses")
    op.drop_table("investment_theses")
