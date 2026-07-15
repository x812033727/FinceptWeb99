"""add retry-safe portfolio transaction import records

Revision ID: 0092
Revises: 0091
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0092"
down_revision: str | None = "0091"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_transaction_imports",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
        ),
        sa.Column(
            "portfolio_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint(
            "portfolio_id", "content_hash",
            name="uq_portfolio_transaction_import_content",
        ),
    )
    op.create_index(
        "ix_portfolio_transaction_imports_portfolio_id",
        "portfolio_transaction_imports", ["portfolio_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_transaction_imports_portfolio_id",
        table_name="portfolio_transaction_imports",
    )
    op.drop_table("portfolio_transaction_imports")
