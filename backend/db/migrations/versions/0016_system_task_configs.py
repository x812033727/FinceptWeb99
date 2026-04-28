"""system_task_configs — admin LLM provider/model overrides for background tasks

Revision ID: 0016
Revises: 0015
Create Date: 2026-04-28

Lets admins choose which LLM provider/model runs each non-chat background
task (news sentiment scoring, discussion-conclusion synthesizer) without
redeploying. Same shape as `persona_overrides` but keyed by an internal
task ID instead of a user-facing persona ID — kept in a separate table so
the persona-routing UI doesn't accidentally surface system tasks as
"agents you can chat with".
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True) if _is_postgres() else sa.String(36)

    op.create_table(
        "system_task_configs",
        sa.Column("task_id", sa.String(length=64), primary_key=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_by_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("system_task_configs")
