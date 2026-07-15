"""add paper fill execution-quality audit fields

Revision ID: 0090
Revises: 0089
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0090"
down_revision: str | None = "0089"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("paper_fills", sa.Column("quote_price", sa.Numeric(18, 6)))
    op.add_column("paper_fills", sa.Column("slippage_bps", sa.Numeric(10, 4)))
    op.add_column("paper_fills", sa.Column("liquidity_quantity", sa.Numeric(18, 6)))
    op.add_column("paper_fills", sa.Column("quote_key", sa.String(length=120)))
    op.add_column(
        "paper_fills",
        sa.Column(
            "execution_source",
            sa.String(length=20),
            server_default="manual",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_paper_fills_quote_price_positive",
        "paper_fills",
        "quote_price IS NULL OR quote_price > 0",
    )
    op.create_check_constraint(
        "ck_paper_fills_slippage_nonnegative",
        "paper_fills",
        "slippage_bps IS NULL OR slippage_bps >= 0",
    )
    op.create_check_constraint(
        "ck_paper_fills_liquidity_positive",
        "paper_fills",
        "liquidity_quantity IS NULL OR liquidity_quantity > 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_paper_fills_liquidity_positive", "paper_fills", type_="check")
    op.drop_constraint("ck_paper_fills_slippage_nonnegative", "paper_fills", type_="check")
    op.drop_constraint("ck_paper_fills_quote_price_positive", "paper_fills", type_="check")
    op.drop_column("paper_fills", "execution_source")
    op.drop_column("paper_fills", "quote_key")
    op.drop_column("paper_fills", "liquidity_quantity")
    op.drop_column("paper_fills", "slippage_bps")
    op.drop_column("paper_fills", "quote_price")
