"""strategy health metrics + auto-promote (PR-4b)

Revision ID: 0053
Revises: 0052
Create Date: 2026-05-07

PR-4a laid the foundation (versioning + maturity tier). PR-4b adds
the active-monitoring layer:

  1. `strategy_health_metrics` table — daily snapshot per strategy,
     hypertable-friendly key shape `(strategy_id, snapshot_date)`.
     Carries rolling-30 brier (raw + calibrated), hit rate, sample
     count, lesson hit rate, the maturity tier at snapshot time, and
     a JSONB `status_flags` array for the UI to surface alerts
     (`brier_drift` / `low_sample` / `lesson_hit_collapse`).

  2. Walk-forward auto-promote knobs on
     `discussion_strategy_templates`:
       - `auto_promote_enabled` BOOLEAN (default FALSE — opt-in)
       - `auto_promote_min_oos_brier_improvement` FLOAT (default 0.0)
       - `auto_promote_min_oos_hit_rate` FLOAT (default 0.5)

When `auto_promote_enabled=True` and the walk-forward orchestrator's
test fold passes both KPIs, Phase 4 writes the test fold's
`weights_override` to the template's live `persona_weights` (and
records a new version via PR-4a's `record_version`). When disabled
or KPIs fail, the test fold result is presented to the operator
for manual review per the existing PR-A2 surface — no behaviour
change for current strategies.

Migration is reversible. Existing rows materialise with
`auto_promote_enabled=FALSE`, preserving the current "always
manual" workflow.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_health_metrics",
        sa.Column(
            "strategy_id",
            postgresql.UUID(as_uuid=True).with_variant(
                sa.String(36), "sqlite",
            ),
            sa.ForeignKey(
                "discussion_strategy_templates.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("brier_30d", sa.Float(), nullable=True),
        sa.Column("calibrated_brier_30d", sa.Float(), nullable=True),
        sa.Column("hit_rate_30d", sa.Float(), nullable=True),
        sa.Column("sample_count_30d", sa.Integer(), nullable=False),
        sa.Column("lesson_hit_rate_30d", sa.Float(), nullable=True),
        sa.Column(
            "maturity_tier_at_snapshot",
            sa.String(16), nullable=True,
        ),
        sa.Column(
            "status_flags",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(
                sa.JSON(), "sqlite",
            ),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint(
            "strategy_id", "snapshot_date",
            name="pk_strategy_health_metrics",
        ),
    )
    op.create_index(
        "ix_strategy_health_metrics_snapshot_date",
        "strategy_health_metrics",
        ["snapshot_date"],
    )

    op.add_column(
        "discussion_strategy_templates",
        sa.Column(
            "auto_promote_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "discussion_strategy_templates",
        sa.Column(
            "auto_promote_min_oos_brier_improvement",
            sa.Float(),
            nullable=False,
            server_default="0.0",
        ),
    )
    op.add_column(
        "discussion_strategy_templates",
        sa.Column(
            "auto_promote_min_oos_hit_rate",
            sa.Float(),
            nullable=False,
            server_default="0.5",
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "discussion_strategy_templates",
        "auto_promote_min_oos_hit_rate",
    )
    op.drop_column(
        "discussion_strategy_templates",
        "auto_promote_min_oos_brier_improvement",
    )
    op.drop_column(
        "discussion_strategy_templates",
        "auto_promote_enabled",
    )
    op.drop_index(
        "ix_strategy_health_metrics_snapshot_date",
        table_name="strategy_health_metrics",
    )
    op.drop_table("strategy_health_metrics")
