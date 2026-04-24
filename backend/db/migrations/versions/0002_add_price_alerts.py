"""add price_alerts table

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-24
"""
from typing import Sequence, Union
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    alert_condition = postgresql.ENUM("above", "below", name="alertcondition")
    alert_condition.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "price_alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("market", sa.String(4), nullable=False),
        sa.Column("condition", sa.Enum("above", "below", name="alertcondition"), nullable=False),
        sa.Column("target_price", sa.Float(), nullable=False),
        sa.Column("triggered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_price_alerts_user_id", "price_alerts", ["user_id"])
    op.create_index(
        "ix_price_alerts_symbol_market_triggered",
        "price_alerts",
        ["symbol", "market", "triggered"],
    )


def downgrade() -> None:
    op.drop_index("ix_price_alerts_symbol_market_triggered", table_name="price_alerts")
    op.drop_index("ix_price_alerts_user_id", table_name="price_alerts")
    op.drop_table("price_alerts")
    op.execute("DROP TYPE IF EXISTS alertcondition")
