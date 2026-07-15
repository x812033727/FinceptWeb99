"""add per-user Email and LINE notification channels

Revision ID: 0082
Revises: 0081
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0082"
down_revision: str | None = "0081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_channels",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("verified", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("destination", sa.String(length=255), nullable=True),
        sa.Column("config", postgresql.JSONB(), nullable=True),
        sa.Column("binding_token_hash", sa.String(length=64), nullable=True),
        sa.Column("binding_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "kind", name="uq_notification_channels_user_kind"),
    )
    op.create_index(
        "ix_notification_channels_user_id", "notification_channels", ["user_id"], unique=False,
    )
    op.create_index(
        "ix_notification_channels_binding_token_hash",
        "notification_channels", ["binding_token_hash"], unique=True,
    )
    op.create_index(
        "ix_notification_channels_destination",
        "notification_channels", ["destination"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_notification_channels_destination", table_name="notification_channels")
    op.drop_index("ix_notification_channels_binding_token_hash", table_name="notification_channels")
    op.drop_index("ix_notification_channels_user_id", table_name="notification_channels")
    op.drop_table("notification_channels")
