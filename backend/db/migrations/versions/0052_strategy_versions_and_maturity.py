"""strategy versions table + maturity tier (PR-4a)

Revision ID: 0052
Revises: 0051
Create Date: 2026-05-07

PR-C / PR-C2 overwrite `discussion_strategy_templates.persona_weights`
and `calibration_curve` directly each time a sweep finishes — the
fit replaces the previous one, with no trail to inspect "what did
this strategy look like a month ago" or roll back to a known-good
state when a new fit underperforms.

PR-4a (lifecycle treasury) introduces a versioned history:

  - `strategy_versions` table: one row per fit attempt of either
    artifact (`weights` or `calibration_curve`), with a per-strategy
    auto-incrementing `version_number`. Each row carries the actual
    payload, sample count, sweep that triggered the fit, and a
    `status` enum (`active` / `superseded` / `rolled_back`).

  - `discussion_strategy_templates.maturity_tier` enum:
    `cold_start` | `learning` | `mature` | `drifting` | `stale`.
    Tells the operator at a glance "is this strategy still trustworthy
    or has it drifted out of regime." Computed by
    `strategy_maturity_service` from sample count, sweep count, and
    rolling-vs-baseline brier deltas.

Behavior change is opt-in: the existing live `calibration_curve` /
`persona_weights` columns are preserved as the "active" pointer.
The version table is additive — services in subsequent commits will
populate it on every fit; this migration just lays the schema.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True).with_variant(
                sa.String(36), "sqlite",
            ),
            primary_key=True,
        ),
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
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "artifact_kind",
            sa.String(32),
            nullable=False,
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(
                sa.JSON(), "sqlite",
            ),
            nullable=False,
        ),
        sa.Column("sample_count", sa.Integer(), nullable=True),
        sa.Column(
            "source_sweep_id",
            postgresql.UUID(as_uuid=True).with_variant(
                sa.String(36), "sqlite",
            ),
            sa.ForeignKey("backtest_sweeps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "fit_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="active",
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "strategy_id", "artifact_kind", "version_number",
            name="uq_strategy_versions_strategy_kind_version",
        ),
    )
    op.create_index(
        "ix_strategy_versions_strategy_kind_status",
        "strategy_versions",
        ["strategy_id", "artifact_kind", "status"],
    )

    # Default 'cold_start' so existing strategies materialise into
    # the right initial bucket; the maturity computation cron will
    # promote them to 'learning' / 'mature' on next run.
    op.add_column(
        "discussion_strategy_templates",
        sa.Column(
            "maturity_tier",
            sa.String(16),
            nullable=False,
            server_default="cold_start",
        ),
    )
    op.add_column(
        "discussion_strategy_templates",
        sa.Column(
            "maturity_computed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("discussion_strategy_templates", "maturity_computed_at")
    op.drop_column("discussion_strategy_templates", "maturity_tier")
    op.drop_index(
        "ix_strategy_versions_strategy_kind_status",
        table_name="strategy_versions",
    )
    op.drop_table("strategy_versions")
