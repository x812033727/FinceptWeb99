"""add point-in-time TW company classification snapshots

Revision ID: 0083
Revises: 0082
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0083"
down_revision: str | None = "0082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tw_company_classification_snapshots",
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("exchange", sa.String(length=10), nullable=False),
        sa.Column("industry", sa.Text(), nullable=True),
        sa.Column("name_zh", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column(
            "captured_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "snapshot_date", "symbol",
            name="pk_tw_company_classification_snapshots",
        ),
    )
    op.create_index(
        "ix_tw_company_classification_lookup",
        "tw_company_classification_snapshots",
        ["symbol", "snapshot_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tw_company_classification_lookup",
        table_name="tw_company_classification_snapshots",
    )
    op.drop_table("tw_company_classification_snapshots")
