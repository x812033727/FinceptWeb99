"""tw_company_info — durable seed for the TW symbol → 公司簡稱 / 產業 map

Revision ID: 0060
Revises: 0059
Create Date: 2026-05-22

Adds a Postgres-backed mirror of the three in-memory dicts
``tw_market_service`` keeps from a successful TWSE refresh (exchange /
industry / 公司簡稱). The Redis snapshot added in commit ``cd6dd3a`` only
helps when at least one prior refresh has populated it; on the first
deploy after that commit (or after a Redis flush) cold-start pods that
can't reach TWSE — the recurring cloud-IP block — end up with empty
maps and the discussion chip card renders bare ticker codes everywhere.

This table is the next tier down: persistent across deploys, shared
across pods via the main DB. ``refresh_symbol_map`` upserts after a
successful TWSE fetch; ``_tw_warmup`` reads from here when Redis is
empty so the chip-name lookup is non-blank from second 0 of every cold
start regardless of upstream reachability.

Schema choices:
  - ``symbol`` is the primary key — one row per listed code.
  - ``exchange`` has a DEFAULT 'TWSE' so insertions that don't specify
    it default to listed (the common case; TPEx codes are a minority).
  - ``industry`` / ``name_zh`` nullable because TWSE's ``t187ap03_L``
    returns missing values for new listings.
  - ``updated_at`` server-defaults to NOW() so the operator can tell
    at a glance how stale the seed is.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tw_company_info",
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column(
            "exchange", sa.String(length=10),
            nullable=False, server_default="TWSE",
        ),
        sa.Column("industry", sa.Text(), nullable=True),
        sa.Column("name_zh", sa.Text(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("symbol", name="pk_tw_company_info"),
    )


def downgrade() -> None:
    op.drop_table("tw_company_info")
