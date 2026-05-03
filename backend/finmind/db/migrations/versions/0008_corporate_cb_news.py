"""corporate actions + convertible bond + news + misc

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-03

Phase 1F+G combined. Sparse event tables (corporate actions) +
4 CB tables + 1 news table.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts_cols() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def _corp_action(name: str, value_cols: list[sa.Column], date_col: str) -> None:
    op.create_table(
        name,
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column(date_col, sa.Date(), nullable=False),
        *value_cols,
        *_ts_cols(),
        sa.PrimaryKeyConstraint("symbol", date_col, name=f"pk_{name}"),
    )


def upgrade() -> None:
    _corp_action(
        "tw_dividend",
        [
            sa.Column("cash_dividend", sa.Numeric(12, 4), nullable=True),
            sa.Column("stock_dividend", sa.Numeric(12, 4), nullable=True),
            sa.Column("ex_dividend_date", sa.Date(), nullable=True),
            sa.Column("ex_rights_date", sa.Date(), nullable=True),
        ],
        "announce_date",
    )
    _corp_action(
        "tw_dividend_result",
        [
            sa.Column("before_price", sa.Numeric(18, 6), nullable=True),
            sa.Column("after_price", sa.Numeric(18, 6), nullable=True),
            sa.Column("cash_dividend", sa.Numeric(12, 4), nullable=True),
            sa.Column("stock_dividend", sa.Numeric(12, 4), nullable=True),
        ],
        "ex_date",
    )
    _corp_action(
        "tw_split",
        [
            sa.Column("before_price", sa.Numeric(18, 6), nullable=True),
            sa.Column("after_price", sa.Numeric(18, 6), nullable=True),
            sa.Column("split_ratio", sa.Numeric(12, 6), nullable=True),
        ],
        "ex_date",
    )
    _corp_action(
        "tw_par_value_change",
        [
            sa.Column("old_par", sa.Numeric(12, 4), nullable=True),
            sa.Column("new_par", sa.Numeric(12, 4), nullable=True),
            sa.Column("reference_price", sa.Numeric(18, 6), nullable=True),
        ],
        "ex_date",
    )
    _corp_action(
        "tw_capital_reduction",
        [
            sa.Column("before_price", sa.Numeric(18, 6), nullable=True),
            sa.Column("after_price", sa.Numeric(18, 6), nullable=True),
            sa.Column("reduction_pct", sa.Numeric(8, 4), nullable=True),
        ],
        "ex_date",
    )
    _corp_action(
        "tw_buyback",
        [
            sa.Column("started_at", sa.Date(), nullable=True),
            sa.Column("ended_at", sa.Date(), nullable=True),
            sa.Column("plan_shares", sa.BigInteger(), nullable=True),
            sa.Column("actual_shares", sa.BigInteger(), nullable=True),
            sa.Column("avg_price", sa.Numeric(18, 6), nullable=True),
        ],
        "announced_at",
    )

    # ── CB ────
    op.create_table(
        "tw_cb_info",
        sa.Column("cb_id", sa.String(length=20), nullable=False),
        sa.Column("underlying_symbol", sa.String(length=20), nullable=True),
        sa.Column("name_zh", sa.String(length=64), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("maturity_date", sa.Date(), nullable=True),
        sa.Column("conversion_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("par_value", sa.Numeric(18, 6), nullable=True),
        *_ts_cols(),
        sa.PrimaryKeyConstraint("cb_id", name="pk_tw_cb_info"),
    )

    for tbl, value_cols in (
        (
            "tw_cb_daily",
            [
                sa.Column("open", sa.Numeric(18, 6), nullable=True),
                sa.Column("high", sa.Numeric(18, 6), nullable=True),
                sa.Column("low", sa.Numeric(18, 6), nullable=True),
                sa.Column("close", sa.Numeric(18, 6), nullable=True),
                sa.Column("volume", sa.BigInteger(), nullable=True),
            ],
        ),
        (
            "tw_cb_inst_daily",
            [
                sa.Column("foreign_buy", sa.BigInteger(), nullable=True),
                sa.Column("foreign_sell", sa.BigInteger(), nullable=True),
                sa.Column("sitc_buy", sa.BigInteger(), nullable=True),
                sa.Column("sitc_sell", sa.BigInteger(), nullable=True),
                sa.Column("dealer_buy", sa.BigInteger(), nullable=True),
                sa.Column("dealer_sell", sa.BigInteger(), nullable=True),
            ],
        ),
        (
            "tw_cb_daily_overview",
            [
                sa.Column("outstanding_amount", sa.Numeric(24, 2), nullable=True),
                sa.Column("converted_shares", sa.BigInteger(), nullable=True),
            ],
        ),
    ):
        op.create_table(
            tbl,
            sa.Column("cb_id", sa.String(length=20), nullable=False),
            sa.Column("ts", sa.Date(), nullable=False),
            *value_cols,
            *_ts_cols(),
            sa.PrimaryKeyConstraint("cb_id", "ts", name=f"pk_{tbl}"),
        )

    # ── News ────
    op.create_table(
        "tw_news_articles",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("link", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sentiment_score", sa.Numeric(6, 4), nullable=True),
        sa.Column("sentiment_label", sa.String(length=16), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tw_news_articles"),
    )
    op.create_index(
        "ix_tw_news_articles_sha256",
        "tw_news_articles",
        ["sha256"],
        unique=True,
    )
    op.create_index(
        "ix_tw_news_articles_market_published",
        "tw_news_articles",
        ["market", "published_at"],
    )
    op.create_index(
        "ix_tw_news_articles_symbol_published",
        "tw_news_articles",
        ["symbol", "published_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tw_news_articles_symbol_published", table_name="tw_news_articles")
    op.drop_index("ix_tw_news_articles_market_published", table_name="tw_news_articles")
    op.drop_index("ix_tw_news_articles_sha256", table_name="tw_news_articles")
    op.drop_table("tw_news_articles")
    for name in (
        "tw_cb_daily_overview",
        "tw_cb_inst_daily",
        "tw_cb_daily",
        "tw_cb_info",
        "tw_buyback",
        "tw_capital_reduction",
        "tw_par_value_change",
        "tw_split",
        "tw_dividend_result",
        "tw_dividend",
    ):
        op.drop_table(name)
