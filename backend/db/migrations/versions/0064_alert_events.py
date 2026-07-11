"""add alert_events table (PR-D5/D4 告警歷史)

Revision ID: 0064
Revises: 0063
Create Date: 2026-07-11

Append-only fired-alert history. `check_and_fire` inserts one row per
firing (same transaction as the `triggered` flag flip) and the
strategy-health monitor inserts kind='strategy_health' rows on a
healthy→degraded transition. Backs `GET /api/alerts/history` and the
21:00 UTC daily email digest cron.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("alert_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("price_alerts.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("market", sa.String(8), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False,
                  server_default="price"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
    )
    # Composite index serves the user-scoped `ORDER BY fired_at DESC`
    # cursor pagination (btree scans backwards for DESC).
    op.create_index(
        "ix_alert_events_user_id_fired_at",
        "alert_events",
        ["user_id", "fired_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_alert_events_user_id_fired_at", table_name="alert_events")
    op.drop_table("alert_events")
