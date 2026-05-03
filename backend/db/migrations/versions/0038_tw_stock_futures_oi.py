"""tw_stock_futures_oi — daily 個股期貨 三大法人未平倉

Revision ID: 0038
Revises: 0037
Create Date: 2026-05-03

Per-stock individual-stock-futures (個股期貨) institutional net-OI
snapshot, fed by FinMind's `TaiwanFuturesInstitutionalInvestors`
dataset (Sponsor tier — `data_id=""` returns all contracts in one
call, no per-symbol fan-out needed).

Distinct from the existing TAIFEX index-level positioning data
(`derivatives_service.get_taifex_positioning` over `TX`):
  - Index futures (TX/MTX/TE/TF) → market-wide directional bias
    on TAIEX as a whole.
  - Stock futures (CDF/CFF/IIF/...) → per-stock smart-money
    conviction. "外資個股期 2330 多單連 5 日增加 +1500 口"
    typically leads the underlying's spot move by 1-2 days
    similar to the index-level pattern.

Schema:
  - `market` always 'TW' for now; future-proofs cross-market.
  - `symbol` is the underlying stock code (e.g. '2330'). Populated
    from FinMind's `stock_id` field when present; falls back to
    the futures contract code (`CDF` etc.) when the underlying
    can't be resolved.
  - `contract_id` is FinMind's `futures_id` so the original code
    is preserved for future-mapping work.
  - One row per (market, symbol, ts) — all three investor types
    (foreign / SITC / dealer) collapsed into separate columns,
    mirroring `tw_institutional_daily`'s shape. The upsert key is
    the PK so re-ingest of the same date overwrites in place.

Read tier: `read_top_foreign_stock_futures_buyers` returns the
top N stocks whose foreign-net-OI grew most over the last K
trading days, fed into the discussion ctx as
`single_stock_futures_oi` so personas can mention "外資個股期
2330 連 5 日多單 +1500 口".
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tw_stock_futures_oi",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("contract_id", sa.String(length=12), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),

        sa.Column("fini_long_oi", sa.BigInteger(), nullable=True),
        sa.Column("fini_short_oi", sa.BigInteger(), nullable=True),
        sa.Column("fini_net_oi", sa.BigInteger(), nullable=True),

        sa.Column("sitc_long_oi", sa.BigInteger(), nullable=True),
        sa.Column("sitc_short_oi", sa.BigInteger(), nullable=True),
        sa.Column("sitc_net_oi", sa.BigInteger(), nullable=True),

        sa.Column("dealer_long_oi", sa.BigInteger(), nullable=True),
        sa.Column("dealer_short_oi", sa.BigInteger(), nullable=True),
        sa.Column("dealer_net_oi", sa.BigInteger(), nullable=True),

        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "ts",
            name="pk_tw_stock_futures_oi",
        ),
    )
    op.create_index(
        "ix_tw_stock_futures_oi_lookup",
        "tw_stock_futures_oi",
        ["market", "ts"],
        unique=False,
    )
    op.create_index(
        "ix_tw_stock_futures_oi_symbol",
        "tw_stock_futures_oi",
        ["market", "symbol", "ts"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tw_stock_futures_oi_symbol",
        table_name="tw_stock_futures_oi",
    )
    op.drop_index(
        "ix_tw_stock_futures_oi_lookup",
        table_name="tw_stock_futures_oi",
    )
    op.drop_table("tw_stock_futures_oi")
