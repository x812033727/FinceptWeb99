"""extend market enum with CRYPTO + widen price_alerts.market

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostgreSQL: extend the enum in-place. ALTER TYPE ... ADD VALUE is the
    # only way; CREATE TYPE + USING cast would lose existing rows. ADD VALUE
    # IF NOT EXISTS is idempotent.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE market ADD VALUE IF NOT EXISTS 'CRYPTO'")
    # SQLite stores enums as strings, no schema change needed.

    # Alert table: 'CRYPTO' is 6 chars, the existing String(4) cap was tight.
    op.alter_column(
        "price_alerts", "market",
        existing_type=sa.String(4),
        type_=sa.String(8),
        existing_nullable=False,
    )


def downgrade() -> None:
    # PostgreSQL doesn't support removing an enum value once added; we narrow
    # the alerts column back and accept the enum extension is permanent.
    op.alter_column(
        "price_alerts", "market",
        existing_type=sa.String(8),
        type_=sa.String(4),
        existing_nullable=False,
    )
