"""add push_subscriptions table (PR-D3 Web Push 瀏覽器推播)

Revision ID: 0068
Revises: 0067
Create Date: 2026-07-12

One row per browser push subscription (a user can hold several — one
per browser/device). `endpoint` is globally unique: the push service
mints one URL per subscription, so subscribe upserts by endpoint and
a re-login on the same browser simply re-binds the row to the new
user. `failed_count` tracks consecutive delivery failures; the
web_push transport prunes rows on 404/410 (subscription expired at
the push service) or after 5 consecutive failures.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False, unique=True),
        sa.Column("keys", postgresql.JSONB(), nullable=False),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_count", sa.Integer(), nullable=False,
                  server_default="0"),
    )
    # The web_push transport loads all of a user's subscriptions per
    # alert firing.
    op.create_index(
        "ix_push_subscriptions_user_id", "push_subscriptions", ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_push_subscriptions_user_id", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")
