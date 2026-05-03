"""signal_audit_history — daily snapshots of citation/hallucination stats

Revision ID: 0034
Revises: 0033
Create Date: 2026-05-03

Persists a daily snapshot of `audit_recent_discussions(days=7)` so
the AdminPage can render trend sparklines per signal. Without this,
the SignalAuditCard only shows the current N-day window with no
sense of whether a signal's citation rate is improving or
deteriorating across deploys.

Schema is one row per (captured_at, market, signal). `captured_at`
is a date so the cron can be safely rerun on the same UTC day
without producing duplicates — the upsert overwrites in place.

`market = NULL` represents the all-markets aggregate; `'TW'` /
`'US'` / `'GLOBAL'` carry market-scoped snapshots so the operator
can drill down per-market without rerunning the audit at read
time.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "signal_audit_history",
        sa.Column("captured_at", sa.Date(), nullable=False),
        # NULL = all-markets aggregate; non-null = market-scoped.
        # Empty string would have worked too but NULL is more
        # truthful and matches the API's `market: str | None`.
        sa.Column("market", sa.String(length=12), nullable=True),
        sa.Column("signal", sa.String(length=64), nullable=False),
        sa.Column(
            "discussions_audited", sa.Integer(),
            nullable=False, server_default="0",
        ),
        sa.Column(
            "present_count", sa.Integer(),
            nullable=False, server_default="0",
        ),
        sa.Column(
            "cited_count", sa.Integer(),
            nullable=False, server_default="0",
        ),
        sa.Column(
            "cited_with_value_count", sa.Integer(),
            nullable=False, server_default="0",
        ),
        sa.Column(
            "hallucinated_count", sa.Integer(),
            nullable=False, server_default="0",
        ),
        sa.Column(
            "persona_count", sa.Integer(),
            nullable=False, server_default="0",
        ),
        sa.Column(
            "persona_count_absent", sa.Integer(),
            nullable=False, server_default="0",
        ),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "captured_at", "market", "signal",
            name="pk_signal_audit_history",
        ),
    )
    # Sparkline reads always pull a single signal across a date
    # range — index by (signal, captured_at) to make that scan
    # cheap.
    op.create_index(
        "ix_signal_audit_history_lookup",
        "signal_audit_history",
        ["signal", "market", "captured_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_signal_audit_history_lookup",
        table_name="signal_audit_history",
    )
    op.drop_table("signal_audit_history")
