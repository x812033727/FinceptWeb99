"""add D1 D5 D20 decision journal

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
        "decision_journal_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("market", sa.String(8), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("prediction_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("stance", sa.String(16), nullable=False, server_default="long"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("outcomes", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("max_drawdown_pct", sa.Float(), nullable=True),
        sa.Column("transaction_cost_bps", sa.Float(), nullable=False, server_default="15"),
        sa.Column("observations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "source_type", "source_id", "symbol", name="uq_decision_journal_source_symbol"),
    )
    op.create_index("ix_decision_journal_user_prediction", "decision_journal_entries", ["user_id", "prediction_at"])


def downgrade() -> None:
    op.drop_index("ix_decision_journal_user_prediction", table_name="decision_journal_entries")
    op.drop_table("decision_journal_entries")
