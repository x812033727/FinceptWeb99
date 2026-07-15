"""add owner-scoped chart drawings

Revision ID: 0081
Revises: 0080
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0081"
down_revision: str | None = "0080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chart_drawings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("market", sa.String(8), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("points", postgresql.JSONB(), nullable=False),
        sa.Column("label", sa.String(80), nullable=True),
        sa.Column("color", sa.String(7), nullable=False, server_default="#f59e0b"),
        sa.Column("alert_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("price_alerts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('horizontal', 'trend')", name="chart_drawings_kind"),
    )
    op.create_index("ix_chart_drawings_owner_symbol", "chart_drawings", ["user_id", "market", "symbol"])
    op.create_index("ix_chart_drawings_alert_id", "chart_drawings", ["alert_id"])


def downgrade() -> None:
    op.drop_index("ix_chart_drawings_alert_id", table_name="chart_drawings")
    op.drop_index("ix_chart_drawings_owner_symbol", table_name="chart_drawings")
    op.drop_table("chart_drawings")
