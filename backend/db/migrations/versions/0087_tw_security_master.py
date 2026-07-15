"""add effective-dated Taiwan security master and trading rules

Revision ID: 0087
Revises: 0086
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0087"
down_revision: str | None = "0086"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tw_security_master_versions",
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("name_zh", sa.Text(), nullable=True),
        sa.Column("exchange", sa.String(length=10), nullable=False),
        sa.Column("instrument_type", sa.String(length=32), nullable=False),
        sa.Column("asset_class", sa.String(length=24), nullable=False),
        sa.Column("is_etf", sa.Boolean(), nullable=False),
        sa.Column("is_bond_etf", sa.Boolean(), nullable=False),
        sa.Column("is_leveraged", sa.Boolean(), nullable=False),
        sa.Column("is_inverse", sa.Boolean(), nullable=False),
        sa.Column("board_lot_size", sa.Integer(), nullable=False),
        sa.Column("odd_lot_size", sa.Integer(), nullable=False),
        sa.Column("sell_tax_bps", sa.Numeric(precision=8, scale=4), nullable=False),
        sa.Column("tax_rule_code", sa.String(length=48), nullable=False),
        sa.Column("classification_source_url", sa.Text(), nullable=True),
        sa.Column("tax_source_url", sa.Text(), nullable=True),
        sa.Column("confidence", sa.String(length=24), nullable=False),
        sa.Column("is_manual_override", sa.Boolean(), nullable=False),
        sa.Column("override_reason", sa.Text(), nullable=True),
        sa.Column("overridden_by", sa.String(length=36), nullable=True),
        sa.Column(
            "captured_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "symbol", "effective_from", "source",
            name="pk_tw_security_master_versions",
        ),
    )
    op.create_index(
        "ix_tw_security_master_effective_lookup",
        "tw_security_master_versions",
        ["symbol", "effective_from", "effective_to"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tw_security_master_effective_lookup",
        table_name="tw_security_master_versions",
    )
    op.drop_table("tw_security_master_versions")
