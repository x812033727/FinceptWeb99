"""add provenance and quality fields to stock reports

Revision ID: 0076
Revises: 0075
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0076"
down_revision: str | None = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("stock_reports", sa.Column("model_id", sa.String(128), nullable=False, server_default=""))
    op.add_column("stock_reports", sa.Column("prompt_version", sa.String(64), nullable=False, server_default="stock-report-v2"))
    op.add_column("stock_reports", sa.Column("data_cutoff", sa.DateTime(timezone=True), nullable=True))
    op.add_column("stock_reports", sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("stock_reports", sa.Column("context_snapshot", postgresql.JSONB(), nullable=True))
    op.add_column("stock_reports", sa.Column("quality_score", sa.Float(), nullable=False, server_default="0"))
    op.execute("UPDATE stock_reports SET model_id = model WHERE model_id = ''")


def downgrade() -> None:
    op.drop_column("stock_reports", "quality_score")
    op.drop_column("stock_reports", "context_snapshot")
    op.drop_column("stock_reports", "evidence")
    op.drop_column("stock_reports", "data_cutoff")
    op.drop_column("stock_reports", "prompt_version")
    op.drop_column("stock_reports", "model_id")
