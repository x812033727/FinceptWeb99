"""discussion_auto_run_configs — per-user opt-in for daily auto-run

Revision ID: 0020
Revises: 0019
Create Date: 2026-04-30

Replaces the single-`ADMIN_EMAIL`-owned auto-run with per-user opt-in.
Each row is a (user_id) PK so a user has exactly zero or one config
record. `enabled=true` rows are picked up by the daily scheduler;
each user gets their own discussion owned by themselves, so the
DiscussionPage sidebar (owner-scoped) renders the auto-run track
record without any permission changes.

`topic` and `rules` are NOT NULL — the user always supplies their own
(no falling back to a hard-coded default). `persona_ids` is JSON list
of 2-8 persona ids; bounds are enforced in the service layer.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True) if _is_postgres() else sa.String(36)

    op.create_table(
        "discussion_auto_run_configs",
        sa.Column(
            "user_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("persona_ids", sa.JSON(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("rules", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Partial-ish index for the scheduler's "find every enabled config"
    # scan. Postgres uses a partial index (WHERE enabled = true); SQLite
    # falls back to a plain index — fine for the test suite.
    if _is_postgres():
        op.create_index(
            "ix_discussion_auto_run_enabled",
            "discussion_auto_run_configs",
            ["enabled"],
            postgresql_where=sa.text("enabled = true"),
        )
    else:
        op.create_index(
            "ix_discussion_auto_run_enabled",
            "discussion_auto_run_configs",
            ["enabled"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_discussion_auto_run_enabled",
        table_name="discussion_auto_run_configs",
    )
    op.drop_table("discussion_auto_run_configs")
