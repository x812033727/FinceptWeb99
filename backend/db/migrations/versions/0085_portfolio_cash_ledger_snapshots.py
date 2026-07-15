"""add append-only portfolio cash ledger and rich EOD snapshots

Revision ID: 0085
Revises: 0084
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0085"
down_revision: str | None = "0084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_cash_entries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("portfolio_id", sa.UUID(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("entry_type", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("transaction_id", sa.UUID(), nullable=True),
        sa.Column("reversal_of", sa.UUID(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("entry_metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_id"], ["portfolios.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"], ["transactions.id"], ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reversal_of"], ["portfolio_cash_entries.id"], ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "portfolio_id", "idempotency_key",
            name="uq_portfolio_cash_entry_idempotency",
        ),
    )
    op.create_index(
        "ix_portfolio_cash_entries_portfolio_id", "portfolio_cash_entries",
        ["portfolio_id"],
    )
    op.create_index(
        "ix_portfolio_cash_entries_transaction_id", "portfolio_cash_entries",
        ["transaction_id"],
    )
    op.create_index(
        "ix_portfolio_cash_entries_reversal_of", "portfolio_cash_entries",
        ["reversal_of"],
    )
    op.create_index(
        "ix_portfolio_cash_entries_lookup", "portfolio_cash_entries",
        ["portfolio_id", "currency", "occurred_on", "created_at"],
    )

    for name, type_ in (
        ("base_currency", sa.String(length=3)),
        ("holdings_value_base", sa.Float()),
        ("cash_value_base", sa.Float()),
        ("total_value_base", sa.Float()),
        ("positions", sa.JSON()),
        ("cash_balances", sa.JSON()),
        ("valuation_quality", sa.JSON()),
    ):
        op.add_column("portfolio_snapshots", sa.Column(name, type_, nullable=True))

    # Old snapshots did not preserve their original base currency or
    # constituents. Keep the USD total usable while marking the limitation.
    op.execute(sa.text("""
        UPDATE portfolio_snapshots
        SET base_currency = 'USD',
            holdings_value_base = total_value_usd,
            cash_value_base = 0,
            total_value_base = total_value_usd,
            positions = '[]',
            cash_balances = '{}',
            valuation_quality = '{"legacy_total_only": true}'
        WHERE base_currency IS NULL
    """))

    # Backfill every legacy security transaction in its native settlement
    # currency. This is SQL (rather than Python row iteration) so offline
    # ``alembic --sql`` deployments contain the exact same data migration.
    op.execute(sa.text("""
        INSERT INTO portfolio_cash_entries (
            id, portfolio_id, currency, amount, entry_type, source,
            occurred_on, transaction_id, reversal_of, idempotency_key,
            notes, entry_metadata, created_at
        )
        SELECT
            uuid_generate_v4(), t.portfolio_id,
            CASE WHEN t.market::text = 'TW' THEN 'TWD' ELSE 'USD' END,
            CASE WHEN t.tx_type::text = 'buy'
                 THEN -(t.quantity * t.price)
                 ELSE  (t.quantity * t.price) END,
            'trade_settlement', 'migration', t.tx_date, t.id, NULL,
            'legacy-transaction:' || t.id::text,
            'Backfilled from pre-ledger transaction',
            json_build_object(
                'legacy_backfill', true, 'tx_type', t.tx_type::text,
                'portfolio_fx_rate', t.fx_rate
            ),
            t.created_at
        FROM transactions t
        WHERE t.quantity * t.price <> 0
        ON CONFLICT (portfolio_id, idempotency_key) DO NOTHING
    """))
    # Existing users did not record deposits. Infer only the smallest opening
    # funding that keeps each native-currency historical balance non-negative;
    # metadata makes the assumption inspectable instead of presenting it as fact.
    op.execute(sa.text("""
        WITH flows AS (
            SELECT
                t.id, t.portfolio_id,
                CASE WHEN t.market::text = 'TW' THEN 'TWD' ELSE 'USD' END AS currency,
                CASE WHEN t.tx_type::text = 'buy'
                     THEN -(t.quantity * t.price)
                     ELSE  (t.quantity * t.price) END AS amount,
                t.tx_date, t.created_at
            FROM transactions t
            WHERE t.quantity * t.price <> 0
        ), running AS (
            SELECT *, SUM(amount) OVER (
                PARTITION BY portfolio_id, currency
                ORDER BY tx_date, created_at, id
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ) AS running_balance
            FROM flows
        ), inferred AS (
            SELECT portfolio_id, currency,
                   MIN(running_balance) AS minimum_balance,
                   MIN(tx_date) AS first_date,
                   MIN(created_at) AS first_created_at
            FROM running
            GROUP BY portfolio_id, currency
        )
        INSERT INTO portfolio_cash_entries (
            id, portfolio_id, currency, amount, entry_type, source,
            occurred_on, transaction_id, reversal_of, idempotency_key,
            notes, entry_metadata, created_at
        )
        SELECT
            uuid_generate_v4(), portfolio_id, currency, -minimum_balance,
            'opening_balance', 'migration', first_date, NULL, NULL,
            'legacy-opening-balance:' || currency,
            'Minimum inferred funding required by pre-ledger transactions',
            json_build_object('legacy_inferred', true), first_created_at
        FROM inferred
        WHERE minimum_balance < 0
        ON CONFLICT (portfolio_id, idempotency_key) DO NOTHING
    """))


def downgrade() -> None:
    for name in (
        "valuation_quality", "cash_balances", "positions", "total_value_base",
        "cash_value_base", "holdings_value_base", "base_currency",
    ):
        op.drop_column("portfolio_snapshots", name)
    op.drop_index("ix_portfolio_cash_entries_lookup", table_name="portfolio_cash_entries")
    op.drop_index("ix_portfolio_cash_entries_reversal_of", table_name="portfolio_cash_entries")
    op.drop_index("ix_portfolio_cash_entries_transaction_id", table_name="portfolio_cash_entries")
    op.drop_index("ix_portfolio_cash_entries_portfolio_id", table_name="portfolio_cash_entries")
    op.drop_table("portfolio_cash_entries")
