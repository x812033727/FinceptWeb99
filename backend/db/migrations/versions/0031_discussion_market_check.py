"""discussions.market + discussion_auto_run_configs.market: CHECK constraint

Revision ID: 0031
Revises: 0030
Create Date: 2026-05-02

PR #214 added the `market` column with `String(8)` and a 'TW' default,
relying on the service layer (`_normalize_market`) to enforce the
allowed-set `{TW, US, GLOBAL}`. Anyone bypassing the service (raw
SQL, ORM unit-test fixtures, future code paths) could still write
`market='JP'` or any other 8-char string, leaving downstream code to
silently degrade (`gather_market_context` falls into the non-TW /
non-US `else` branch and produces a near-empty ctx).

This migration adds a DB-level CHECK so the contract is enforced
where the data lives. If a third market is added later (e.g. `EU`,
`HK`), the CHECK can be widened with a follow-up migration.

Existing rows: PR #214's server_default backfilled every row to 'TW',
so the conversion succeeds with no data fixup needed.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table renders to ALTER TABLE on Postgres and to
    # the create-new-table-copy-data-rename pattern on SQLite — so
    # the same migration code runs on both dialects without a
    # dialect branch.
    with op.batch_alter_table("discussions") as batch_op:
        batch_op.create_check_constraint(
            "ck_discussions_market_allowed",
            "market IN ('TW', 'US', 'GLOBAL')",
        )
    with op.batch_alter_table("discussion_auto_run_configs") as batch_op:
        batch_op.create_check_constraint(
            "ck_discussion_auto_run_configs_market_allowed",
            "market IN ('TW', 'US', 'GLOBAL')",
        )


def downgrade() -> None:
    with op.batch_alter_table("discussions") as batch_op:
        batch_op.drop_constraint(
            "ck_discussions_market_allowed", type_="check",
        )
    with op.batch_alter_table("discussion_auto_run_configs") as batch_op:
        batch_op.drop_constraint(
            "ck_discussion_auto_run_configs_market_allowed", type_="check",
        )
