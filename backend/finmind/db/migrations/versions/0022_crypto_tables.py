"""crypto tables — ohlcv + universe + asset_info + funding + open interest

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-12

W5. Cryptocurrency data pipeline (Binance OHLCV / funding / open
interest + CoinGecko universe & market-cap info). The three time-series
tables (crypto_ohlcv, crypto_funding_rate, crypto_open_interest) are
hypertable-promoted with compression; the two dimension tables
(crypto_universe, crypto_asset_info) stay plain (low volume, queried by
key not by time).

Chunk interval is 7 days (not the 1-month TW default) because 1h bars
across a top-200 universe put far more rows per day into crypto_ohlcv.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
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


def _promote_hypertable(name: str, time_col: str, segmentby: str) -> None:
    """Hypertable + compression for a crypto time-series table. Guarded
    by `_has_timescaledb()` so the SQLite test DB and any non-Timescale
    Postgres just keep a plain table + PK index."""
    if not _has_timescaledb():
        return
    op.execute(
        f"SELECT create_hypertable('{name}', '{time_col}', "
        f"chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE)"
    )
    op.execute(
        f"ALTER TABLE {name} SET ("
        f"  timescaledb.compress,"
        f"  timescaledb.compress_segmentby = '{segmentby}',"
        f"  timescaledb.compress_orderby = '{time_col} DESC')"
    )
    # Compress chunks older than 30 days — recent data stays
    # uncompressed for fast incremental UPSERT, history gets packed.
    op.execute(
        f"SELECT add_compression_policy('{name}', "
        f"INTERVAL '30 days', if_not_exists => TRUE)"
    )


def upgrade() -> None:
    # ── crypto_ohlcv (hypertable) ────────────────────────────────
    op.create_table(
        "crypto_ohlcv",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("interval", sa.String(length=4), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(24, 8), nullable=True),
        sa.Column("high", sa.Numeric(24, 8), nullable=True),
        sa.Column("low", sa.Numeric(24, 8), nullable=True),
        sa.Column("close", sa.Numeric(24, 8), nullable=True),
        sa.Column("volume", sa.Numeric(28, 8), nullable=True),
        sa.Column("quote_volume", sa.Numeric(28, 8), nullable=True),
        sa.Column("trades", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "interval", "ts", name="pk_crypto_ohlcv"
        ),
    )
    op.create_index(
        "ix_crypto_ohlcv_symbol_interval_ts",
        "crypto_ohlcv",
        ["symbol", "interval", sa.text("ts DESC")],
    )
    _promote_hypertable("crypto_ohlcv", "ts", "symbol,interval")

    # ── crypto_universe (dimension) ──────────────────────────────
    op.create_table(
        "crypto_universe",
        sa.Column("coingecko_id", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=True),
        sa.Column("binance_symbol", sa.String(length=24), nullable=True),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("market_cap_rank", sa.BigInteger(), nullable=True),
        sa.Column("added_at", sa.Date(), nullable=True),
        sa.Column("removed_at", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("coingecko_id", name="pk_crypto_universe"),
    )
    op.create_index(
        "ix_crypto_universe_status_rank",
        "crypto_universe",
        ["status", "market_cap_rank"],
    )

    # ── crypto_asset_info (weekly snapshot) ──────────────────────
    op.create_table(
        "crypto_asset_info",
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("coingecko_id", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=True),
        sa.Column("market_cap_rank", sa.BigInteger(), nullable=True),
        sa.Column("market_cap", sa.Numeric(30, 2), nullable=True),
        sa.Column("circulating_supply", sa.Numeric(30, 4), nullable=True),
        sa.Column("total_supply", sa.Numeric(30, 4), nullable=True),
        sa.Column("ath", sa.Numeric(24, 8), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "snapshot_date", "coingecko_id", name="pk_crypto_asset_info"
        ),
    )

    # ── crypto_funding_rate (hypertable) ─────────────────────────
    op.create_table(
        "crypto_funding_rate",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("funding_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("funding_rate", sa.Numeric(18, 10), nullable=True),
        sa.Column("mark_price", sa.Numeric(24, 8), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "funding_time", name="pk_crypto_funding_rate"
        ),
    )
    op.create_index(
        "ix_crypto_funding_rate_symbol_time",
        "crypto_funding_rate",
        ["symbol", sa.text("funding_time DESC")],
    )
    _promote_hypertable("crypto_funding_rate", "funding_time", "symbol")

    # ── crypto_open_interest (hypertable) ────────────────────────
    op.create_table(
        "crypto_open_interest",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_interest", sa.Numeric(28, 8), nullable=True),
        sa.Column("open_interest_value", sa.Numeric(30, 2), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "ts", name="pk_crypto_open_interest"
        ),
    )
    op.create_index(
        "ix_crypto_open_interest_symbol_ts",
        "crypto_open_interest",
        ["symbol", sa.text("ts DESC")],
    )
    _promote_hypertable("crypto_open_interest", "ts", "symbol")


def downgrade() -> None:
    for name in (
        "crypto_open_interest",
        "crypto_funding_rate",
        "crypto_asset_info",
        "crypto_universe",
        "crypto_ohlcv",
    ):
        op.drop_table(name)
