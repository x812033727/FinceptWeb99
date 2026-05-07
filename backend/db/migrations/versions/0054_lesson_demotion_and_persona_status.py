"""lesson demotion + persona status (PR-4c)

Revision ID: 0054
Revises: 0053
Create Date: 2026-05-07

PR-4c closes the lifecycle treasury's last open loop — the one
that was "lessons only go up the tier, never down; persona weights
shrink to ~0 but the persona still gets called every round."

Two surfaces:

  1. `discussion_lessons` gains:
     - `archived_at` TIMESTAMP TZ NULLABLE — soft-delete for
       lessons that haven't been used in 60+ days. Preserved for
       audit (operators can manually un-archive).
     - `demoted_at` TIMESTAMP TZ NULLABLE — when a `semantic`
       lesson dropped back to `episodic` because its rolling
       hit-rate fell below the demotion threshold.
     - `recent_hit_rate_10` FLOAT NULLABLE — moving hit rate
       over the most recent 10 usages. Computed by the cron + read
       by `demote_stale_lessons`.

  2. `discussion_strategy_templates` gains:
     - `persona_status` JSONB DEFAULT '{}' — `{persona_id:
       'active'|'frozen'|'shadow'}`. Frozen personas are excluded
       from the round roster; shadow personas run but don't
       influence the synthesizer (placeholder for the PR-B5 shadow
       mode).
     - `persona_status_updated_at` TIMESTAMP TZ NULLABLE — when
       the map was last touched, used by the timeline view.

Auto-freeze logic lives in service code (PR-4c's
`persona_status_service.auto_freeze_underperformers`) and runs
in sweep Phase 3 alongside the existing weight learner. The schema
is intentionally minimal — keep all the policy in code where it's
testable.

Migration is reversible. Existing rows materialise with all new
fields NULL / `{}`, preserving current behaviour.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── discussion_lessons additions ────────────────────────────
    op.add_column(
        "discussion_lessons",
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "discussion_lessons",
        sa.Column(
            "demoted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "discussion_lessons",
        sa.Column(
            "recent_hit_rate_10",
            sa.Float(),
            nullable=True,
        ),
    )
    # Index speeds the cron's "find lessons that need archive
    # check" scan: WHERE archived_at IS NULL AND last_used_at <
    # cutoff. Existing index on (owner, market) handles the
    # primary read path.
    op.create_index(
        "ix_discussion_lessons_archived_at",
        "discussion_lessons",
        ["archived_at"],
    )

    # ── discussion_strategy_templates additions ────────────────
    op.add_column(
        "discussion_strategy_templates",
        sa.Column(
            "persona_status",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(
                sa.JSON(), "sqlite",
            ),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "discussion_strategy_templates",
        sa.Column(
            "persona_status_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "discussion_strategy_templates",
        "persona_status_updated_at",
    )
    op.drop_column("discussion_strategy_templates", "persona_status")
    op.drop_index(
        "ix_discussion_lessons_archived_at",
        table_name="discussion_lessons",
    )
    op.drop_column("discussion_lessons", "recent_hit_rate_10")
    op.drop_column("discussion_lessons", "demoted_at")
    op.drop_column("discussion_lessons", "archived_at")
