"""add backtest_runs table (C3 回測結果持久化與比較)

Revision ID: 0067
Revises: 0066
Create Date: 2026-07-12

Persisted backtest runs. `POST /api/analytics/backtest` with
`save=true` inserts one row here; the AnalyticsPage 歷史回測 list reads
it back via `GET /api/analytics/backtest-runs` and side-by-side
comparison uses `GET /api/analytics/backtest-runs/compare?ids=…`.

`trades` is capped at the last 500 trade dicts (NULL when the run made
no trades); when truncated, `config.trades_truncated` is true and
`metrics.total_trades` keeps the full count.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("name", sa.String(120), nullable=True),
        sa.Column("strategy", sa.String(64), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("config", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("metrics", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("equity_curve", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("trades", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    # User-scoped history list — `ORDER BY created_at DESC` per user.
    op.create_index(
        "ix_backtest_runs_user_id_created_at",
        "backtest_runs",
        ["user_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_backtest_runs_user_id_created_at",
                  table_name="backtest_runs")
    op.drop_table("backtest_runs")
