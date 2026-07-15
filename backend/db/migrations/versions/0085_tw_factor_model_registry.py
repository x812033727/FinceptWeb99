"""add TW factor research runs and model registry

Revision ID: 0085
Revises: 0084
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0085"
down_revision: str | None = "0084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tw_factor_research_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column("profile", sa.String(length=24), nullable=False),
        sa.Column("methodology_version", sa.String(length=64), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("gate_result", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tw_factor_research_user_created", "tw_factor_research_runs",
        ["user_id", "created_at"],
    )
    op.create_table(
        "tw_factor_model_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("profile", sa.String(length=24), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("methodology_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("weights", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("gate_result", sa.JSON(), nullable=False),
        sa.Column("source_run_id", sa.UUID(), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promotion_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_run_id"], ["tw_factor_research_runs.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "profile", "version_number",
            name="uq_tw_factor_model_user_profile_version",
        ),
    )
    op.create_index(
        "ix_tw_factor_model_user_profile_status", "tw_factor_model_versions",
        ["user_id", "profile", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tw_factor_model_user_profile_status",
        table_name="tw_factor_model_versions",
    )
    op.drop_table("tw_factor_model_versions")
    op.drop_index(
        "ix_tw_factor_research_user_created",
        table_name="tw_factor_research_runs",
    )
    op.drop_table("tw_factor_research_runs")
