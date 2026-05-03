"""enable TimescaleDB compression on every hypertable

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-03

Phase 1I. Wraps every hypertable created in 0004-0009 with:

  ALTER TABLE … SET (timescaledb.compress, segmentby='market, symbol', orderby='ts DESC');
  SELECT add_compression_policy(table, INTERVAL '30 days');

Expected ratios (per the design doc): daily ~22×, minute ~18×,
tick ~15×, broker_daily_report ~20×. Total compressed footprint
shrinks from ~424 GB raw to ~22 GB across the full Phase 1 schema
with 10-year backfill.

Skipped silently when:
  - The DB isn't Postgres (SQLite tests).
  - TimescaleDB extension isn't installed (vanilla PG).
A no-op migration in those cases lets the schema stay in lockstep
across environments.

Compression caveat: 30-day-old chunks become read-mostly. Live
ingest into the current chunk continues normally; historical
back-rewrites (e.g. FinMind correcting a 60-day-old revenue
figure) need explicit decompress_chunk + reinsert + recompress
per the design doc — not handled here.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
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


# (table_name, segmentby, retention_days_before_compress)
# Tick + broker_daily compress aggressively (7 days) because their
# row volume per chunk is enormous and compressed reads stay fast.
# Daily-grain tables compress at 30 days — gives operators a month
# to spot-fix any ingest-time mistakes before the chunk freezes.
COMPRESSION_PLAN: tuple[tuple[str, str, str], ...] = (
    # Phase 1B
    ("ohlcv_daily",                   "market, symbol", "30 days"),
    ("tw_stock_price_adj",            "market, symbol", "30 days"),
    ("tw_stock_per_daily",            "market, symbol", "30 days"),
    ("tw_day_trade_daily",            "market, symbol", "30 days"),
    ("tw_total_return_index",         "market, symbol", "30 days"),
    ("tw_price_limit_daily",          "market, symbol", "30 days"),
    ("tw_market_value_weight",        "market, symbol", "30 days"),
    # Phase 1C
    ("tw_margin_daily",               "market, symbol", "30 days"),
    ("tw_institutional_daily",        "market, symbol", "30 days"),
    ("tw_day_trade_fee",              "market, symbol", "30 days"),
    ("tw_foreign_shareholding",       "market, symbol", "30 days"),
    ("tw_total_margin_daily",         "market",         "60 days"),
    ("tw_total_inst_daily",           "market",         "60 days"),
    ("tw_short_sale_balance_daily",   "market",         "60 days"),
    ("tw_margin_maintenance",         "market",         "60 days"),
    ("tw_loan_collateral",            "market",         "60 days"),
    ("tw_govt_bank_flow",             "market",         "60 days"),
    ("tw_broker_daily_report",        "market, symbol", "7 days"),
    # Phase 1D
    ("tw_market_value",               "market, symbol", "30 days"),
    # Phase 1E
    ("tw_futures_daily",              "contract",       "30 days"),
    ("tw_option_daily",               "contract",       "30 days"),
    # Phase 1H
    ("tw_stock_minute",               "market, symbol", "7 days"),
    ("tw_stock_tick",                 "market, symbol", "7 days"),
)


def upgrade() -> None:
    if not _has_timescaledb():
        return

    bind = op.get_bind()
    for table, segmentby, retention in COMPRESSION_PLAN:
        # Skip silently if the hypertable doesn't exist — happens when
        # downgrade runs out of order or in tests.
        exists = bind.exec_driver_sql(
            "SELECT 1 FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = %s",
            (table,),
        ).scalar()
        if not exists:
            continue

        op.execute(
            sa.text(
                f"ALTER TABLE {table} SET ("
                f"  timescaledb.compress,"
                f"  timescaledb.compress_segmentby = '{segmentby}',"
                f"  timescaledb.compress_orderby = 'ts DESC'"
                f")"
            )
        )
        op.execute(
            sa.text(
                f"SELECT add_compression_policy('{table}', "
                f"INTERVAL '{retention}', if_not_exists => TRUE)"
            )
        )


def downgrade() -> None:
    if not _has_timescaledb():
        return

    bind = op.get_bind()
    for table, _segmentby, _retention in COMPRESSION_PLAN:
        exists = bind.exec_driver_sql(
            "SELECT 1 FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = %s",
            (table,),
        ).scalar()
        if not exists:
            continue

        op.execute(
            sa.text(
                f"SELECT remove_compression_policy('{table}', if_exists => TRUE)"
            )
        )
        # Decompress all chunks before turning off compression — required
        # by TimescaleDB.
        op.execute(
            sa.text(
                f"SELECT decompress_chunk(c, true) "
                f"FROM show_chunks('{table}') c"
            )
        )
        op.execute(
            sa.text(
                f"ALTER TABLE {table} SET (timescaledb.compress = false)"
            )
        )
