"""widen `tw_loan_collateral` to carry the full FinMind response

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-05

The original schema (`market`, `ts`, `collateral_balance`) was a
placeholder — only one numeric column for what FinMind actually
ships as 34 distinct margin/loan/securities-finance balance fields
per (stock, date). The catalog had `TaiwanStockLoanCollateralBalance`
flagged as one of the "schema mismatch" gaps that couldn't be
ingested without a wider local table.

FinMind's `TaiwanStockLoanCollateralBalance` returns one row per
(stock, date) with five product groups, each with the same lifecycle
fields (PreviousDayBalance / Buy / Sell / CashRedemption / [Replacement]
/ CurrentDayBalance / NextDayQuota):

  - Margin                          (融資)            — 6 fields
  - SecuritiesFirmLoan              (券商借券)        — 7 fields
  - UnrestrictedLoan                (一般借券)        — 7 fields
  - SecuritiesFinanceSecuredLoan    (集保有擔保借券)  — 7 fields
  - SettlementMargin                (交割融資)        — 7 fields

Total: 34 numeric fields + (market, symbol, ts) = 37-column response.

Drops the old table (zero rows in production — confirmed before
running the migration) and recreates with the wide schema. Hypertable
+ compression policy match the rest of the daily-grain finmind
tables. Skipped on SQLite because the previous schema also was — the
test suite uses a separate fixture.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NUMERIC_FIELDS = (
    # Margin (融資) — 6 fields, no Replacement
    "margin_previous_day_balance",
    "margin_buy",
    "margin_sell",
    "margin_cash_redemption",
    "margin_current_day_balance",
    "margin_next_day_quota",
    # SecuritiesFirmLoan (券商借券) — 7 fields
    "securities_firm_loan_previous_day_balance",
    "securities_firm_loan_buy",
    "securities_firm_loan_sell",
    "securities_firm_loan_cash_redemption",
    "securities_firm_loan_replacement",
    "securities_firm_loan_current_day_balance",
    "securities_firm_loan_next_day_quota",
    # UnrestrictedLoan (一般借券) — 7 fields
    "unrestricted_loan_previous_day_balance",
    "unrestricted_loan_buy",
    "unrestricted_loan_sell",
    "unrestricted_loan_cash_redemption",
    "unrestricted_loan_replacement",
    "unrestricted_loan_current_day_balance",
    "unrestricted_loan_next_day_quota",
    # SecuritiesFinanceSecuredLoan (集保有擔保借券) — 7 fields
    "securities_finance_secured_loan_previous_day_balance",
    "securities_finance_secured_loan_buy",
    "securities_finance_secured_loan_sell",
    "securities_finance_secured_loan_cash_redemption",
    "securities_finance_secured_loan_replacement",
    "securities_finance_secured_loan_current_day_balance",
    "securities_finance_secured_loan_next_day_quota",
    # SettlementMargin (交割融資) — 7 fields
    "settlement_margin_previous_day_balance",
    "settlement_margin_buy",
    "settlement_margin_sell",
    "settlement_margin_cash_redemption",
    "settlement_margin_replacement",
    "settlement_margin_current_day_balance",
    "settlement_margin_next_day_quota",
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    # CASCADE drops the dependent compression policy (created in 0010)
    # and any constraints/indexes — the table has 0 rows in prod, no
    # data loss to worry about.
    op.execute("DROP TABLE IF EXISTS tw_loan_collateral CASCADE")

    cols = [
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
    ]
    for f in _NUMERIC_FIELDS:
        cols.append(sa.Column(f, sa.BigInteger(), nullable=True))
    cols.extend([
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "market", "symbol", "ts", name="pk_tw_loan_collateral"
        ),
    ])
    op.create_table("tw_loan_collateral", *cols)

    # Hypertable + compression policy match the rest of the daily-grain
    # finmind tables (see migration 0010). 30-day compression budget.
    has_ts = bool(
        op.get_bind()
        .exec_driver_sql(
            "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"
        )
        .scalar()
    )
    if has_ts:
        op.execute(
            "SELECT create_hypertable('tw_loan_collateral', 'ts', "
            "chunk_time_interval => INTERVAL '1 month', "
            "if_not_exists => TRUE)"
        )
        op.execute(
            "ALTER TABLE tw_loan_collateral SET ("
            "  timescaledb.compress,"
            "  timescaledb.compress_segmentby = 'market, symbol',"
            "  timescaledb.compress_orderby = 'ts DESC'"
            ")"
        )
        op.execute(
            "SELECT add_compression_policy('tw_loan_collateral', "
            "INTERVAL '30 days', if_not_exists => TRUE)"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("DROP TABLE IF EXISTS tw_loan_collateral CASCADE")
    # Recreate the original placeholder shape (single numeric column).
    op.create_table(
        "tw_loan_collateral",
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("collateral_balance", sa.Numeric(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("market", "ts", name="pk_tw_loan_collateral"),
    )
