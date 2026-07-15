"""index portfolio transaction history queries

Revision ID: 0094
Revises: 0093
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0094"
down_revision: str | None = "0093"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_transactions_portfolio_history",
        "transactions",
        ["portfolio_id", "tx_date", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_transactions_portfolio_history", table_name="transactions",
    )
