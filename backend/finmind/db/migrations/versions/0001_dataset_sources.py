"""dataset_sources — FinMind dataset routing table

Revision ID: 0001
Revises:
Create Date: 2026-05-03

First migration of the FinMind clone DB. Creates the `dataset_sources`
routing table that the ingest layer reads to decide where to write each
FinMind dataset's payload and which upstream source to consult.

Schema choices:
  - PK = `dataset_code` (= the FinMind dataset name) so seed UPSERT
    is idempotent and a typo can never produce two rows for the same
    dataset.
  - `local_table` is `NOT NULL DEFAULT ''` rather than nullable so
    queries can `WHERE local_table != ''` without `IS NOT NULL`
    juggling.
  - `enabled` defaults to FALSE — Phase 1 will flip rows on as
    destination tables get migrated. Prevents the ingest scheduler
    from racing ahead of schema availability.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dataset_sources",
        sa.Column("dataset_code", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("description_zh", sa.String(length=128), nullable=False),
        sa.Column(
            "local_table",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column("per_symbol", sa.Boolean(), nullable=False),
        sa.Column("primary_source", sa.String(length=16), nullable=False),
        sa.Column("fallback_source", sa.String(length=16), nullable=True),
        sa.Column("active_source", sa.String(length=16), nullable=False),
        sa.Column(
            "sponsor_tier",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("ingest_freq", sa.String(length=16), nullable=False),
        sa.Column("last_ingest_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ingest_rows", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("dataset_code", name="pk_dataset_sources"),
    )
    op.create_index(
        "ix_dataset_sources_category_enabled",
        "dataset_sources",
        ["category", "enabled"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_dataset_sources_category_enabled", table_name="dataset_sources"
    )
    op.drop_table("dataset_sources")
