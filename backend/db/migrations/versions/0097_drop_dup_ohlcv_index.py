"""drop ix_ohlcv_daily_lookup — duplicate of the primary key

Revision ID: 0097
Revises: 0096

`ix_ohlcv_daily_lookup` indexes (market, symbol, ts) — the exact
primary-key tuple in the same order — so it's a second identical
B-tree on the largest table in the deployment: pure write
amplification on every ingest upsert plus wasted disk. Every read
path (read_ohlcv_range and friends) filters on the PK columns and is
served by the PK index.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0097"
down_revision: str | None = "0096"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_ohlcv_daily_lookup", table_name="ohlcv_daily")


def downgrade() -> None:
    op.create_index(
        "ix_ohlcv_daily_lookup",
        "ohlcv_daily",
        ["market", "symbol", "ts"],
    )
