"""tw_short_sale_balance_daily: market-wide -> per-symbol

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-06

The original `tw_short_sale_balance_daily` table (created in 0005) was
shaped for a market-wide aggregate (PK ``(market, ts)``, two columns:
``short_balance``, ``short_quota``). That assumption was wrong: FinMind's
``TaiwanDailyShortSaleBalances`` endpoint actually returns one row per
symbol with two parallel short-sale channels (Margin融券 + SBL借券),
each with previous-day balance / today's short / covering / quota /
current balance — 13 numeric fields per ``(market, symbol, ts)``.

Result: the dataset never had an ingest mapping (the wide-row shape
didn't match the narrow target), the runner recorded every claimed
chunk as ``skipped`` with "no ingest mapping for ...", and the table
sat empty since 0005.

Fix: drop the empty table, recreate per-symbol with the full FinMind
column set, flip ``dataset_sources.per_symbol`` to true, and re-add
the timescaledb compression policy with a per-symbol segmentby key.
The companion mapping in `finmind/ingest/mappings.py` makes the runner
actually populate the table.

Safe because the table is provably empty (verified 2026-05-06: 0 rows,
no foreign keys reference it, no service code reads from it — the only
references are the create in 0005 and the compression policy in 0010).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_timescaledb() -> bool:
    if op.get_bind().dialect.name != "postgresql":
        return False
    return bool(
        op.get_bind()
        .exec_driver_sql(
            "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"
        )
        .scalar()
    )


_VALUE_COLUMNS = [
    sa.Column("margin_prev_balance",   sa.BigInteger(), nullable=True),
    sa.Column("margin_short_sales",    sa.BigInteger(), nullable=True),
    sa.Column("margin_short_covering", sa.BigInteger(), nullable=True),
    sa.Column("margin_stock_redemption", sa.BigInteger(), nullable=True),
    sa.Column("margin_balance",        sa.BigInteger(), nullable=True),
    sa.Column("margin_quota",          sa.BigInteger(), nullable=True),
    sa.Column("sbl_prev_balance",      sa.BigInteger(), nullable=True),
    sa.Column("sbl_short_sales",       sa.BigInteger(), nullable=True),
    sa.Column("sbl_short_covering",    sa.BigInteger(), nullable=True),
    sa.Column("sbl_returns",           sa.BigInteger(), nullable=True),
    sa.Column("sbl_adjustments",       sa.BigInteger(), nullable=True),
    sa.Column("sbl_balance",           sa.BigInteger(), nullable=True),
    sa.Column("sbl_quota",             sa.BigInteger(), nullable=True),
]


def upgrade() -> None:
    # Defensive: if compression was ever turned on (it never was at the
    # time of writing — table empty since 0005 — but stay idempotent),
    # the policy is owned by the table and goes away with the drop.
    op.drop_table("tw_short_sale_balance_daily")

    op.create_table(
        "tw_short_sale_balance_daily",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        *_VALUE_COLUMNS,
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "ts",
            name="pk_tw_short_sale_balance_daily",
        ),
    )
    op.create_index(
        "ix_tw_short_sale_balance_daily_symbol_ts",
        "tw_short_sale_balance_daily",
        ["symbol", sa.text("ts DESC")],
    )

    if _has_timescaledb():
        op.execute(
            "SELECT create_hypertable('tw_short_sale_balance_daily', 'ts', "
            "chunk_time_interval => INTERVAL '1 month', "
            "if_not_exists => TRUE)"
        )
        op.execute(
            "ALTER TABLE tw_short_sale_balance_daily SET ("
            "  timescaledb.compress, "
            "  timescaledb.compress_segmentby = 'market, symbol', "
            "  timescaledb.compress_orderby   = 'ts DESC'"
            ")"
        )
        # Match the policy granted to the same table in 0010 — 60 days
        # after which a chunk becomes eligible for compression.
        op.execute(
            "SELECT add_compression_policy("
            "  'tw_short_sale_balance_daily', INTERVAL '60 days', "
            "  if_not_exists => TRUE)"
        )

    op.execute(
        "UPDATE dataset_sources SET per_symbol = true "
        "WHERE dataset_code = 'TaiwanDailyShortSaleBalances'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE dataset_sources SET per_symbol = false "
        "WHERE dataset_code = 'TaiwanDailyShortSaleBalances'"
    )
    op.drop_table("tw_short_sale_balance_daily")
    op.create_table(
        "tw_short_sale_balance_daily",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("short_balance", sa.BigInteger(), nullable=True),
        sa.Column("short_quota", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "ts", name="pk_tw_short_sale_balance_daily"
        ),
    )
    if _has_timescaledb():
        op.execute(
            "SELECT create_hypertable('tw_short_sale_balance_daily', 'ts', "
            "chunk_time_interval => INTERVAL '6 months', "
            "if_not_exists => TRUE)"
        )
