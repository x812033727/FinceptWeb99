"""link imported transactions to their source batch

Revision ID: 0093
Revises: 0092
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0093"
down_revision: str | None = "0092"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("import_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_transactions_import_id",
        "transactions",
        "portfolio_transaction_imports",
        ["import_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_transactions_import_id", "transactions", ["import_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_import_id", table_name="transactions")
    op.drop_constraint(
        "fk_transactions_import_id", "transactions", type_="foreignkey",
    )
    op.drop_column("transactions", "import_id")
