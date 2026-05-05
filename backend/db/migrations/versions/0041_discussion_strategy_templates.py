"""discussion_strategy_templates — reusable sweep templates

Revision ID: 0041
Revises: 0040
Create Date: 2026-05-05

A "strategy" packages the topic + rules + persona roster + default
runtime knobs (rounds, concurrency, auto_post_mortem) so the
operator can re-launch the same sweep without retyping. PR-B's
aggregation dashboard groups per-strategy across sweeps; PR-C's
weight learner writes per-persona weights back into
`persona_weights` so the next sweep using this template can apply
the learned weights when synthesising.

Owner-scoped: each operator's strategies are private. Soft-delete
via `deleted_at` so historical sweeps that referenced a deleted
template can still resolve `template.name` for display. The
sweep's `strategy_id` FK uses ON DELETE SET NULL (no soft-delete
cascade — we want the sweep to outlive the template removal).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discussion_strategy_templates",
        sa.Column(
            "id", UUID(as_uuid=True),
            primary_key=True, nullable=False,
        ),
        sa.Column(
            "owner_id", UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("rules", sa.Text(), nullable=False),
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column(
            "persona_ids", sa.JSON(),
            nullable=False, server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "default_rounds", sa.Integer(),
            nullable=False, server_default="1",
        ),
        sa.Column(
            "default_concurrency", sa.Integer(),
            nullable=False, server_default="1",
        ),
        sa.Column(
            "default_auto_post_mortem", sa.Boolean(),
            nullable=False, server_default=sa.text("true"),
        ),
        # Per-persona weight written by PR-C's learner. Shape:
        # {"persona_id": float}. Empty means "treat all personas
        # equally" (legacy behaviour).
        sa.Column(
            "persona_weights", sa.JSON(),
            nullable=False, server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "weights_updated_at", sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.Column(
            "deleted_at", sa.DateTime(timezone=True), nullable=True,
        ),
    )
    op.create_index(
        "ix_strategy_templates_owner_active",
        "discussion_strategy_templates",
        ["owner_id", "deleted_at"],
        unique=False,
    )

    # Sweep gains an optional FK to its source template. SET NULL on
    # delete so sweep history survives template removal.
    op.add_column(
        "backtest_sweeps",
        sa.Column(
            "strategy_id", UUID(as_uuid=True),
            sa.ForeignKey(
                "discussion_strategy_templates.id",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_backtest_sweeps_strategy",
        "backtest_sweeps",
        ["strategy_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_backtest_sweeps_strategy",
        table_name="backtest_sweeps",
    )
    op.drop_column("backtest_sweeps", "strategy_id")
    op.drop_index(
        "ix_strategy_templates_owner_active",
        table_name="discussion_strategy_templates",
    )
    op.drop_table("discussion_strategy_templates")
