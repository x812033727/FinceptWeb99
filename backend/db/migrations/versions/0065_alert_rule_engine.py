"""price_alerts 規則引擎欄位 (PR-D1 進階告警)

Revision ID: 0065
Revises: 0064
Create Date: 2026-07-11

Extends `price_alerts` from a fixed above/below price comparator into
a rule engine row: `condition_type` selects an evaluator from
`services/alert_rules.py`, `params` (JSONB) carries per-type knobs
(pct / lookback_days / multiple / days), and `repeat` +
`cooldown_seconds` + `last_fired_at` implement re-fire semantics.

Data migration: existing rows keep working — the legacy `condition`
enum (above/below, semantics: fire when price >= / <= target_price)
maps 1:1 onto the new type names via `_CONDITION_TYPE_MAP`, and
`last_fired_at` is backfilled from `triggered_at` so cooldown math
has a baseline. Legacy `condition` / `target_price` columns are kept
(relaxed to nullable — non-price rule types have no target price).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Legacy enum value → condition_type. Legacy semantics (verified in
# services/alert_service.py pre-0065): above = fire when
# current_price >= target_price, below = fire when <= target_price —
# exactly the price_above / price_below evaluators.
_CONDITION_TYPE_MAP: dict[str, str] = {
    "above": "price_above",
    "below": "price_below",
}

_LEGACY_ENUM = sa.Enum("above", "below", name="alertcondition")


def _mapping_statements() -> list[str]:
    """UPDATE statements backfilling condition_type from the legacy
    enum. Kept as plain generated SQL (portable text comparison) so
    the data-mapping is unit-testable against SQLite."""
    return [
        (
            f"UPDATE price_alerts SET condition_type = '{new}' "
            f"WHERE condition = '{old}'"
        )
        for old, new in _CONDITION_TYPE_MAP.items()
    ]


def upgrade() -> None:
    op.add_column("price_alerts", sa.Column(
        "condition_type", sa.String(32), nullable=False,
        server_default="price_above",
    ))
    op.add_column("price_alerts", sa.Column(
        "params", postgresql.JSONB(), nullable=True,
    ))
    op.add_column("price_alerts", sa.Column(
        "cooldown_seconds", sa.Integer(), nullable=False,
        server_default="0",
    ))
    op.add_column("price_alerts", sa.Column(
        "repeat", sa.Boolean(), nullable=False,
        server_default=sa.text("false"),
    ))
    op.add_column("price_alerts", sa.Column(
        "last_fired_at", sa.DateTime(timezone=True), nullable=True,
    ))

    # Backfill existing rows: above→price_above, below→price_below.
    for stmt in _mapping_statements():
        op.execute(stmt)
    # Already-fired rows get a cooldown baseline.
    op.execute(
        "UPDATE price_alerts SET last_fired_at = triggered_at "
        "WHERE triggered_at IS NOT NULL"
    )

    # Non-price rule types carry no target price / legacy enum value.
    op.alter_column(
        "price_alerts", "condition",
        existing_type=_LEGACY_ENUM, nullable=True,
    )
    op.alter_column(
        "price_alerts", "target_price",
        existing_type=sa.Float(), nullable=True,
    )


def downgrade() -> None:
    # Rows created by non-price rule types would violate the restored
    # NOT NULLs — give them sentinel legacy values first.
    op.execute("UPDATE price_alerts SET condition = 'above' WHERE condition IS NULL")
    op.execute("UPDATE price_alerts SET target_price = 0 WHERE target_price IS NULL")
    op.alter_column(
        "price_alerts", "condition",
        existing_type=_LEGACY_ENUM, nullable=False,
    )
    op.alter_column(
        "price_alerts", "target_price",
        existing_type=sa.Float(), nullable=False,
    )
    op.drop_column("price_alerts", "last_fired_at")
    op.drop_column("price_alerts", "repeat")
    op.drop_column("price_alerts", "cooldown_seconds")
    op.drop_column("price_alerts", "params")
    op.drop_column("price_alerts", "condition_type")
