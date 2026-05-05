"""replace `tw_futopt_daily_info` placeholder with `tw_futopt_master`

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-05

The catalog routed `TaiwanFutOptDailyInfo` to `tw_futopt_daily_info`,
which the model docstring described as a "market overview" — daily
aggregates of futures+options volume + open interest. But FinMind's
actual `TaiwanFutOptDailyInfo` response is **master/info data**, not
aggregates:

    {'code': 'AAA', 'type': 'TaiwanOptionDaily', 'name': '南亞1000股選擇權'}
    {'code': 'TX',  'type': 'TaiwanFuturesDaily', 'name': '臺股期貨'}
    ...

One row per (futures_or_option contract code), used as the universe
for per-symbol fan-out into `TaiwanFuturesDaily` / `TaiwanOptionDaily`.
1,323 rows total today (1056 futures contracts + 267 options).

The placeholder table had 0 rows in production (verified before
running the migration) and no other dataset writes to it, so the
clean fix is to drop it entirely and replace with `tw_futopt_master`
shaped for the actual FinMind response. The catalog row in
`dataset_sources` is updated in the same transaction so the runner
picks up the new local_table on the next chunk claim.

The orphaned `TwFutoptDailyInfo` ORM class is left in place for now —
it doesn't reference the table at runtime (everything goes through
the runner's text-SQL UPSERT path), and a follow-up cleanup can
delete the model. Keeping it here keeps this migration scoped to the
schema change.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("DROP TABLE IF EXISTS tw_futopt_daily_info CASCADE")
    op.create_table(
        "tw_futopt_master",
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("name_zh", sa.String(length=128), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("code", name="pk_tw_futopt_master"),
    )
    # Keep `dataset_sources` in sync so the runner routes the next
    # chunk to the new local_table without needing a re-seed.
    op.execute(
        "UPDATE dataset_sources SET local_table='tw_futopt_master' "
        "WHERE dataset_code='TaiwanFutOptDailyInfo'"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("DROP TABLE IF EXISTS tw_futopt_master CASCADE")
    op.create_table(
        "tw_futopt_daily_info",
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("futures_volume", sa.BigInteger(), nullable=True),
        sa.Column("options_volume", sa.BigInteger(), nullable=True),
        sa.Column("futures_open_interest", sa.BigInteger(), nullable=True),
        sa.Column("options_open_interest", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("ts", name="pk_tw_futopt_daily_info"),
    )
    op.execute(
        "UPDATE dataset_sources SET local_table='tw_futopt_daily_info' "
        "WHERE dataset_code='TaiwanFutOptDailyInfo'"
    )
