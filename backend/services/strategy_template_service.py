"""Strategy template CRUD (PR-A).

A template captures the operator's sweep recipe so the
BacktestSweepCard can offer a "load from strategy" picker. PR-B
groups sweep aggregates per template; PR-C writes
`persona_weights` here so future sweeps using the same template
can apply learned weights.

All operations are owner-scoped — the API layer is a thin shell
that maps `current_user` -> `owner_id` and forwards.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion_strategy_template import DiscussionStrategyTemplate

# Validation bounds — match BacktestSweep so a template can always
# be applied without runtime rejection.
MAX_NAME_LEN = 120
MAX_ROUNDS = 5
MAX_CONCURRENCY = 3
ALLOWED_MARKETS = ("TW", "US", "GLOBAL")


def _validate_schedule(
    *,
    cadence_hours: int,
    anchor_offset_days: int,
    trading_days_count: int,
) -> None:
    if cadence_hours < 1 or cadence_hours > 720:
        raise ValueError(
            f"auto_schedule_cadence_hours must be in [1, 720], "
            f"got {cadence_hours}"
        )
    if anchor_offset_days < -30 or anchor_offset_days > 0:
        raise ValueError(
            f"auto_schedule_anchor_offset_days must be in [-30, 0], "
            f"got {anchor_offset_days}"
        )
    if trading_days_count < 1 or trading_days_count > 30:
        raise ValueError(
            f"auto_schedule_trading_days_count must be in [1, 30], "
            f"got {trading_days_count}"
        )


def _validate_common(
    *,
    name: str,
    topic: str,
    rules: str,
    market: str,
    persona_ids: list[str],
    default_rounds: int,
    default_concurrency: int,
) -> None:
    if not name.strip():
        raise ValueError("name is required")
    if len(name) > MAX_NAME_LEN:
        raise ValueError(f"name must be <= {MAX_NAME_LEN} chars")
    if not topic.strip() or not rules.strip():
        raise ValueError("topic and rules are required")
    if market not in ALLOWED_MARKETS:
        raise ValueError(
            f"market must be one of {ALLOWED_MARKETS}, got {market!r}"
        )
    if not persona_ids:
        raise ValueError("persona_ids must not be empty")
    if default_rounds < 1 or default_rounds > MAX_ROUNDS:
        raise ValueError(f"default_rounds must be in [1, {MAX_ROUNDS}]")
    if default_concurrency < 1 or default_concurrency > MAX_CONCURRENCY:
        raise ValueError(
            f"default_concurrency must be in [1, {MAX_CONCURRENCY}]"
        )


async def create_template(
    db: AsyncSession,
    *,
    owner_id: UUID,
    name: str,
    description: str | None,
    topic: str,
    rules: str,
    market: str,
    persona_ids: list[str],
    default_rounds: int = 1,
    default_concurrency: int = 1,
    default_auto_post_mortem: bool = True,
    auto_schedule_enabled: bool = False,
    auto_schedule_cadence_hours: int = 24,
    auto_schedule_anchor_offset_days: int = -1,
    auto_schedule_trading_days_count: int = 1,
) -> DiscussionStrategyTemplate:
    _validate_common(
        name=name, topic=topic, rules=rules, market=market,
        persona_ids=persona_ids,
        default_rounds=default_rounds,
        default_concurrency=default_concurrency,
    )
    _validate_schedule(
        cadence_hours=auto_schedule_cadence_hours,
        anchor_offset_days=auto_schedule_anchor_offset_days,
        trading_days_count=auto_schedule_trading_days_count,
    )
    tmpl = DiscussionStrategyTemplate(
        owner_id=owner_id,
        name=name.strip(),
        description=(description or None),
        topic=topic.strip(),
        rules=rules.strip(),
        market=market,
        persona_ids=list(persona_ids),
        default_rounds=default_rounds,
        default_concurrency=default_concurrency,
        default_auto_post_mortem=default_auto_post_mortem,
        auto_schedule_enabled=auto_schedule_enabled,
        auto_schedule_cadence_hours=auto_schedule_cadence_hours,
        auto_schedule_anchor_offset_days=auto_schedule_anchor_offset_days,
        auto_schedule_trading_days_count=auto_schedule_trading_days_count,
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


async def list_templates(
    db: AsyncSession, *, owner_id: UUID, include_deleted: bool = False,
) -> list[DiscussionStrategyTemplate]:
    """Most-recent-first list of an owner's templates. Soft-deleted
    rows are hidden by default (operators rarely want them in the
    picker)."""
    stmt = select(DiscussionStrategyTemplate).where(
        DiscussionStrategyTemplate.owner_id == owner_id,
    )
    if not include_deleted:
        stmt = stmt.where(DiscussionStrategyTemplate.deleted_at.is_(None))
    stmt = stmt.order_by(DiscussionStrategyTemplate.created_at.desc())
    return list((await db.scalars(stmt)).all())


async def get_template(
    db: AsyncSession,
    *,
    template_id: UUID,
    owner_id: UUID,
    include_deleted: bool = False,
) -> DiscussionStrategyTemplate | None:
    """Owner-scoped fetch. PR-B's aggregator passes
    `include_deleted=True` so a sweep that points at a soft-deleted
    template can still resolve its name."""
    stmt = select(DiscussionStrategyTemplate).where(
        DiscussionStrategyTemplate.id == template_id,
        DiscussionStrategyTemplate.owner_id == owner_id,
    )
    if not include_deleted:
        stmt = stmt.where(DiscussionStrategyTemplate.deleted_at.is_(None))
    return await db.scalar(stmt)


async def update_template(
    db: AsyncSession,
    tmpl: DiscussionStrategyTemplate,
    *,
    patch: dict[str, Any],
) -> DiscussionStrategyTemplate:
    """Apply partial updates. Re-validates the merged shape so a
    rules edit doesn't silently leave the template uncreatable."""
    merged = {
        "name": tmpl.name,
        "topic": tmpl.topic,
        "rules": tmpl.rules,
        "market": tmpl.market,
        "persona_ids": list(tmpl.persona_ids or []),
        "default_rounds": tmpl.default_rounds,
        "default_concurrency": tmpl.default_concurrency,
    }
    merged.update({k: v for k, v in patch.items() if k in merged})
    _validate_common(**merged)

    schedule_keys = {
        "auto_schedule_cadence_hours": tmpl.auto_schedule_cadence_hours,
        "auto_schedule_anchor_offset_days": tmpl.auto_schedule_anchor_offset_days,
        "auto_schedule_trading_days_count": tmpl.auto_schedule_trading_days_count,
    }
    schedule_keys.update({
        k: v for k, v in patch.items() if k in schedule_keys
    })
    _validate_schedule(
        cadence_hours=schedule_keys["auto_schedule_cadence_hours"],
        anchor_offset_days=schedule_keys["auto_schedule_anchor_offset_days"],
        trading_days_count=schedule_keys["auto_schedule_trading_days_count"],
    )

    for k, v in patch.items():
        if k == "description":
            tmpl.description = v or None
        elif k == "default_auto_post_mortem":
            tmpl.default_auto_post_mortem = bool(v)
        elif k == "auto_schedule_enabled":
            tmpl.auto_schedule_enabled = bool(v)
        elif k in merged or k in schedule_keys:
            setattr(tmpl, k, v)

    tmpl.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


async def soft_delete_template(
    db: AsyncSession, tmpl: DiscussionStrategyTemplate,
) -> DiscussionStrategyTemplate:
    """Soft-delete so historical sweeps that referenced this
    template still resolve `template.name` for the dashboard."""
    if tmpl.deleted_at is not None:
        return tmpl
    tmpl.deleted_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


async def set_persona_weights(
    db: AsyncSession,
    tmpl: DiscussionStrategyTemplate,
    *,
    weights: dict[str, float],
) -> DiscussionStrategyTemplate:
    """PR-C entry point. Replaces the weights map and stamps
    `weights_updated_at` so the synthesizer knows how fresh the
    learned weights are."""
    tmpl.persona_weights = dict(weights)
    tmpl.weights_updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


__all__ = [
    "MAX_NAME_LEN", "MAX_ROUNDS", "MAX_CONCURRENCY", "ALLOWED_MARKETS",
    "create_template", "list_templates", "get_template",
    "update_template", "soft_delete_template", "set_persona_weights",
]
