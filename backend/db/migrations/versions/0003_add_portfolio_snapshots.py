"""add portfolio_snapshots table

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-24
"""
from typing import Sequence, Union
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("total_value_usd", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "portfolio_id", "snapshot_date",
            name="uq_portfolio_snapshots_portfolio_id_snapshot_date",
        ),
    )
    op.create_index("ix_portfolio_snapshots_portfolio_id", "portfolio_snapshots", ["portfolio_id"])
    op.create_index(
        "ix_portfolio_snapshots_date",
        "portfolio_snapshots",
        ["portfolio_id", "snapshot_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_portfolio_snapshots_date", table_name="portfolio_snapshots")
    op.drop_index("ix_portfolio_snapshots_portfolio_id", table_name="portfolio_snapshots")
    op.drop_table("portfolio_snapshots")
