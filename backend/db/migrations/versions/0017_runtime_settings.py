"""runtime_settings — admin-tunable env-var overrides

Revision ID: 0017
Revises: 0016
Create Date: 2026-04-28

A small key/value store for env-var-equivalent settings that admins
should be able to retune without redeploying. Schema is intentionally
minimal: `key` is the primary key (matches the env-var name), `value`
is JSON-typed so we can store ints, floats, bools, or strings without
column branching, plus the usual audit columns.

The set of *allowed* keys is enforced in `runtime_config_service._REGISTRY`,
not at the DB layer — that lets us add / remove / rename runtime
tunables in code without churning migrations every time.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True) if _is_postgres() else sa.String(36)

    op.create_table(
        "runtime_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
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
    op.drop_table("runtime_settings")
