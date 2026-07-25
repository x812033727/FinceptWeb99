"""Timescale compression on the chip/price hypertables (spec Track 3a)

Revision ID: 0099
Revises: 0098

Rows are NEVER deleted — compression only. 90-day threshold keeps every
chunk the daily ingest walks (10-day lookback) and the discussion reads
(30-day history windows) uncompressed; only cold history compresses.

Requires TimescaleDB community license (prod: `timescale`, 2.26.3 —
verified). No plain-Postgres guard is added here: `ALTER TABLE ... SET
(timescaledb.compress ...)` and `add_compression_policy` already fail
loudly with a clear Postgres error if the extension/hypertable is
absent, which is what we want on a non-Timescale environment (unit-test
SQLite never runs alembic at all).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0099"
down_revision: str | None = "0098"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("ohlcv_daily", "tw_institutional_daily", "tw_margin_daily")


def upgrade() -> None:
    for t in _TABLES:
        op.execute(
            f"ALTER TABLE {t} SET ("
            f"timescaledb.compress, "
            f"timescaledb.compress_segmentby = 'market,symbol', "
            f"timescaledb.compress_orderby = 'ts DESC')"
        )
        op.execute(
            f"SELECT add_compression_policy('{t}', INTERVAL '90 days')"
        )


def downgrade() -> None:
    for t in _TABLES:
        op.execute(
            f"SELECT remove_compression_policy('{t}', if_exists => true)"
        )
        op.execute(
            f"SELECT decompress_chunk(c, true) "
            f"FROM show_chunks('{t}') c"
        )
        op.execute(f"ALTER TABLE {t} SET (timescaledb.compress = false)")
