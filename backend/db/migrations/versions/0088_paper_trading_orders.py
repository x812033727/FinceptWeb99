"""add paper-trading order lifecycle and fills

Revision ID: 0088
Revises: 0087
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0088"
down_revision: str | None = "0087"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("market", sa.String(length=10), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("order_type", sa.String(length=10), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(18, 6), server_default="0", nullable=False),
        sa.Column("limit_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("reservation_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("average_fill_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("fee_bps", sa.Numeric(10, 4), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("side IN ('buy', 'sell')", name="ck_paper_orders_side"),
        sa.CheckConstraint("order_type IN ('market', 'limit')", name="ck_paper_orders_order_type"),
        sa.CheckConstraint(
            "status IN ('pending', 'partially_filled', 'filled', 'cancelled')",
            name="ck_paper_orders_status",
        ),
        sa.CheckConstraint("quantity > 0", name="ck_paper_orders_quantity_positive"),
        sa.CheckConstraint(
            "filled_quantity >= 0 AND filled_quantity <= quantity",
            name="ck_paper_orders_fill_quantity",
        ),
        sa.CheckConstraint("reservation_price > 0", name="ck_paper_orders_reservation_price"),
        sa.CheckConstraint("fee_bps >= 0", name="ck_paper_orders_fee_bps"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_paper_orders"),
        sa.UniqueConstraint(
            "portfolio_id", "idempotency_key", name="uq_paper_orders_portfolio_idempotency"
        ),
    )
    op.create_index("ix_paper_orders_portfolio_id", "paper_orders", ["portfolio_id"])
    op.create_index(
        "ix_paper_orders_portfolio_status_created",
        "paper_orders",
        ["portfolio_id", "status", "created_at"],
    )
    op.create_table(
        "paper_fills",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("price", sa.Numeric(18, 6), nullable=False),
        sa.Column("fee", sa.Numeric(18, 6), server_default="0", nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_paper_fills_quantity_positive"),
        sa.CheckConstraint("price > 0", name="ck_paper_fills_price_positive"),
        sa.CheckConstraint("fee >= 0", name="ck_paper_fills_fee_nonnegative"),
        sa.ForeignKeyConstraint(["order_id"], ["paper_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_paper_fills"),
        sa.UniqueConstraint("order_id", "idempotency_key", name="uq_paper_fills_order_idempotency"),
        sa.UniqueConstraint("transaction_id", name="uq_paper_fills_transaction_id"),
    )
    op.create_index("ix_paper_fills_order_id", "paper_fills", ["order_id"])
    op.create_index("ix_paper_fills_order_filled", "paper_fills", ["order_id", "filled_at"])


def downgrade() -> None:
    op.drop_index("ix_paper_fills_order_filled", table_name="paper_fills")
    op.drop_index("ix_paper_fills_order_id", table_name="paper_fills")
    op.drop_table("paper_fills")
    op.drop_index("ix_paper_orders_portfolio_status_created", table_name="paper_orders")
    op.drop_index("ix_paper_orders_portfolio_id", table_name="paper_orders")
    op.drop_table("paper_orders")
