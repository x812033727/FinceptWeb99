"""add traceable daily stock pick runs

Revision ID: 0080
Revises: 0079
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0080"
down_revision: str | None = "0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stock_pick_runs",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("market", sa.String(8), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column(
            "methodology_version", sa.String(64), nullable=False,
            server_default="trusted-report-ranking-v1",
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="completed"),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "candidates", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "source_report_ids", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "generated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "user_id", "market", "run_date",
            name="uq_stock_pick_run_user_market_date",
        ),
    )
    op.create_index(
        "ix_stock_pick_runs_user_generated",
        "stock_pick_runs", ["user_id", "generated_at"],
    )
    op.create_index("ix_stock_pick_runs_user_id", "stock_pick_runs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_stock_pick_runs_user_id", table_name="stock_pick_runs")
    op.drop_index("ix_stock_pick_runs_user_generated", table_name="stock_pick_runs")
    op.drop_table("stock_pick_runs")
