"""tw_institutional_daily + tw_margin_daily — chip-metric archives

Revision ID: 0021
Revises: 0020
Create Date: 2026-04-30

Adds two daily archives to back the read tier for TW chip metrics:

  - `tw_institutional_daily` — 法人買賣超 (foreign / SITC / dealer
    buy/sell volumes) per (symbol, ts).
  - `tw_margin_daily` — 融資融券 (margin purchase / balance, short
    sale / balance) per (symbol, ts).

Two TimescaleDB hypertables with the same names already exist in
`docker/postgres/init.sql` from an earlier draft of the schema, but
no code was written to populate them and they used `time TIMESTAMPTZ`
instead of `ts DATE`. Rather than rename or migrate the ghost
tables, we use `_daily` suffixes here — same convention as
`ohlcv_daily` (PR #11) — and let the ghost ones quietly age out.

Schema choices mirror migration 0011 (ohlcv_daily):
  - Composite PK (market, symbol, ts) so daily ingest is an idempotent
    UPSERT and TimescaleDB's "partitioning column must appear in every
    UNIQUE index" rule is satisfied.
  - `market` is in the PK from day one so future cross-market
    extensions don't need a schema migration.
  - Hypertable conversion is wrapped in a TimescaleDB-presence check
    so plain PostgreSQL + SQLite (test env) work unchanged.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _has_timescaledb() -> bool:
    if not _is_postgres():
        return False
    return bool(op.get_bind().exec_driver_sql(
        "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"
    ).scalar())


def upgrade() -> None:
    # ── 法人買賣超 ──────────────────────────────────────────────
    op.create_table(
        "tw_institutional_daily",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("fini_buy", sa.BigInteger(), nullable=True),
        sa.Column("fini_sell", sa.BigInteger(), nullable=True),
        sa.Column("sitc_buy", sa.BigInteger(), nullable=True),
        sa.Column("sitc_sell", sa.BigInteger(), nullable=True),
        sa.Column("dealer_buy", sa.BigInteger(), nullable=True),
        sa.Column("dealer_sell", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "ts", name="pk_tw_institutional_daily",
        ),
    )
    op.create_index(
        "ix_tw_institutional_daily_lookup",
        "tw_institutional_daily",
        ["market", "symbol", "ts"],
        unique=False,
    )

    if _has_timescaledb():
        op.execute(
            "SELECT create_hypertable("
            "'tw_institutional_daily', 'ts', "
            "chunk_time_interval => INTERVAL '3 months', "
            "if_not_exists => TRUE)"
        )

    # ── 融資融券 ──────────────────────────────────────────────────
    op.create_table(
        "tw_margin_daily",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("margin_purchase", sa.BigInteger(), nullable=True),
        sa.Column("margin_balance", sa.BigInteger(), nullable=True),
        sa.Column("short_sale", sa.BigInteger(), nullable=True),
        sa.Column("short_balance", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "ts", name="pk_tw_margin_daily",
        ),
    )
    op.create_index(
        "ix_tw_margin_daily_lookup",
        "tw_margin_daily",
        ["market", "symbol", "ts"],
        unique=False,
    )

    if _has_timescaledb():
        op.execute(
            "SELECT create_hypertable("
            "'tw_margin_daily', 'ts', "
            "chunk_time_interval => INTERVAL '3 months', "
            "if_not_exists => TRUE)"
        )


def downgrade() -> None:
    op.drop_index("ix_tw_margin_daily_lookup", table_name="tw_margin_daily")
    op.drop_table("tw_margin_daily")
    op.drop_index(
        "ix_tw_institutional_daily_lookup", table_name="tw_institutional_daily",
    )
    op.drop_table("tw_institutional_daily")
