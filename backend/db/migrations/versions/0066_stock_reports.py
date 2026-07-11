"""add stock_reports table (B1 個股 AI 研究報告)

Revision ID: 0066
Revises: 0065
Create Date: 2026-07-11

Archive of AI-generated per-symbol research reports.
`POST /api/ai/stock-report/{market}/{symbol}` streams the report over
SSE and inserts one row here on successful completion; the panel's
history list reads it back via `GET /api/ai/stock-reports`.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stock_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("market", sa.String(8), nullable=False),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column("model", sa.String(128), nullable=False,
                  server_default=""),
        sa.Column("sections", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    # User-scoped history list — `ORDER BY created_at DESC` per user.
    op.create_index(
        "ix_stock_reports_user_id_created_at",
        "stock_reports",
        ["user_id", sa.text("created_at DESC")],
    )
    # Per-symbol history for the StockDetailPage panel list.
    op.create_index(
        "ix_stock_reports_symbol_market_created_at",
        "stock_reports",
        ["symbol", "market", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_stock_reports_symbol_market_created_at",
                  table_name="stock_reports")
    op.drop_index("ix_stock_reports_user_id_created_at",
                  table_name="stock_reports")
    op.drop_table("stock_reports")
