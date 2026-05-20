"""discussion_auto_run_configs — send_email opt-in

Revision ID: 0059
Revises: 0058
Create Date: 2026-05-19

Adds a `send_email` BOOLEAN column to `discussion_auto_run_configs`.
When true, the daily auto-run cron emails the user a Markdown report
of the resulting discussion (topic / rules / per-round turns /
synthesized conclusion) after a successful run. Default false so
existing opt-in users see no behaviour change.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussion_auto_run_configs",
        sa.Column(
            "send_email",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("discussion_auto_run_configs", "send_email")
