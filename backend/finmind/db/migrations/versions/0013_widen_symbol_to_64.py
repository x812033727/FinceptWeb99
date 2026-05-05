"""widen `symbol` to varchar(64) across every finmind table

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-05

FinMind's `TaiwanStockPrice` market-wide query returns rows for both
individual tickers (4–6 chars) AND TWSE/OTC sector aggregates whose
codes are descriptive English (e.g. `TradingConsumersGoods`, 22
chars; `OtherElectronic`, etc.). The original `varchar(20)` symbol
column overflows on those sector rows, surfacing as
`StringDataRightTruncationError` mid-INSERT and rolling back the
entire chunk transaction — so even the per-ticker rows in the same
batch get lost.

Widen `symbol` to `varchar(64)` across every finmind table that has
one. PG treats `ALTER COLUMN TYPE varchar(N)` length-increase as a
metadata-only change since 9.2, so this is fast even on the empty
hypertables (`ohlcv_daily`, `tw_stock_minute`, `tw_stock_tick`).

Picking 64 (vs. e.g. 32): FinMind sector aggregates are documentation
strings, not codes — bounded by what the data team writes. 64 leaves
3× headroom past the longest known label without going to TEXT.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Every finmind table with a `symbol VARCHAR(20)` column as of 0012.
# Generated via:
#   SELECT table_name FROM information_schema.columns
#    WHERE table_schema='finmind' AND column_name='symbol'
#      AND character_maximum_length=20 ORDER BY table_name;
_TABLES = (
    "backfill_progress",
    "ohlcv_daily",
    "tw_balance_sheet",
    "tw_block_trade",
    "tw_broker_daily_report",
    "tw_buyback",
    "tw_capital_reduction",
    "tw_cash_flow",
    "tw_day_trade_daily",
    "tw_day_trade_fee",
    "tw_delisting",
    "tw_disposition",
    "tw_dividend",
    "tw_dividend_result",
    "tw_foreign_shareholding",
    "tw_holdings_aggregates",
    "tw_income_statement",
    "tw_industry_chain",
    "tw_institutional_daily",
    "tw_margin_daily",
    "tw_market_value",
    "tw_market_value_weight",
    "tw_news_articles",
    "tw_par_value_change",
    "tw_price_limit_daily",
    "tw_revenue_monthly",
    "tw_securities_lending",
    "tw_short_sale_suspension",
    "tw_split",
    "tw_stock_info",
    "tw_stock_minute",
    "tw_stock_per_daily",
    "tw_stock_price_adj",
    "tw_stock_tick",
    "tw_suspended",
    "tw_total_return_index",
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for tbl in _TABLES:
        op.alter_column(tbl, "symbol", type_=sa.String(64))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # Length DECREASE rewrites the table; only run if you've verified
    # no row exceeds 20 chars (sector aggregates from FinMind WILL).
    for tbl in _TABLES:
        op.alter_column(tbl, "symbol", type_=sa.String(20))
