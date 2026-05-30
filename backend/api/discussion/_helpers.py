"""Shared utilities for the discussion API package.

Hosts the cross-cutting building blocks that every sub-router needs:
  - `CurrentUser` annotated dependency (viewer-or-above auth)
  - quota reservation (`_check_quota`) + refund (`_refund`)
  - owner-UUID coercion (`_coerce_owner_uuid`)
  - ORM → response serializers (`_to_response`, `_sweep_to_response`,
    `_template_to_response`)
  - `_BG_ROUND_TASKS` for detached SSE round tasks (module-level so the
    task object isn't GC'd when the originating request completes its
    StreamingResponse but the SSE consumer disconnects early)

`router.py` re-exports these so existing test mock paths like
`patch("api.discussion.router._refund", ...)` keep working — the
binding in `router.py` is the canonical one tests target. Sub-routers
that need quota / serialization import directly from this module.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Annotated

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_viewer
from cache.redis_cache import cache_decr, cache_incr, key_ai_counter
from config import settings
from models.discussion import Discussion
from services.discussion.symbol_names import enrich_conclusion_with_names

from api.discussion.schemas import (
    BacktestSweepFailedDate,
    BacktestSweepResponse,
    DiscussionResponse,
    StrategyTemplateResponse,
)

log = logging.getLogger(__name__)

CurrentUser = Annotated[dict, Depends(require_viewer)]

# Detached background tasks for in-flight rounds. Kept module-level so
# tasks aren't garbage-collected when the originating request returns
# its StreamingResponse — the SSE consumer might disconnect long before
# the round actually completes, and we want the task to live until
# `run_round` finishes persisting turns + status reset + refund.
_BG_ROUND_TASKS: dict[uuid.UUID, asyncio.Task] = {}


# ── quota ──────────────────────────────────────────────────────────


async def _daily_limit(db: AsyncSession, role: str) -> int:
    """Resolve the user's daily quota via runtime_config_service so an
    admin can retune AI_REQUESTS_VIEWER_DAILY / ANALYST_DAILY from the
    UI without redeploying. Falls back to the compiled default on any
    resolver failure."""
    key = "AI_REQUESTS_ANALYST_DAILY" if role in ("analyst", "admin") \
        else "AI_REQUESTS_VIEWER_DAILY"
    try:
        from services.runtime_config_service import get_int as _get_int
        return await _get_int(db, key)
    except Exception:
        return getattr(settings, key)


async def _check_quota(user: dict, db: AsyncSession, *, cost: int) -> None:
    """Reserve `cost` requests against the daily counter atomically.

    Done as a sequential INCR loop so two concurrent rounds can't both
    squeak under the limit (the final new_count check rejects whichever
    one crosses). If the post-increment count exceeds the cap we refund
    and reject.
    """
    # Admins are exempt from the daily AI quota — they're the operator, and
    # the quota exists to cap viewer / analyst spend. Skip the counter
    # entirely so admin multi-round runs / sweeps never hit a 429.
    if user.get("role") == "admin":
        return
    limit = await _daily_limit(db, user.get("role", "viewer"))
    new_count = 0
    for _ in range(cost):
        new_count = await cache_incr(key_ai_counter(user["id"]), ttl_seconds=86400)
    if new_count > limit:
        for _ in range(cost):
            await cache_decr(key_ai_counter(user["id"]))
        raise HTTPException(
            status_code=429,
            detail=(
                f"Daily AI quota exceeded ({limit} requests/day). "
                "Resets at midnight UTC."
            ),
        )


async def _refund(user: dict, *, count: int) -> None:
    # Mirror `_check_quota`: admins never incremented the counter, so there's
    # nothing to refund. Returning early keeps the admin counter from drifting
    # negative on an early-aborted round.
    if user.get("role") == "admin":
        return
    for _ in range(count):
        try:
            await cache_decr(key_ai_counter(user["id"]))
        except Exception as exc:
            log.error(
                "discussion.quota.refund_failed",
                extra={"user_id": user.get("id"), "error": str(exc)},
            )
            return


def _coerce_owner_uuid(user: dict) -> uuid.UUID:
    raw = user.get("id")
    if isinstance(raw, uuid.UUID):
        return raw
    return uuid.UUID(str(raw))


def _to_response(d: Discussion) -> DiscussionResponse:
    # Inject company-name lookups into both conclusion shapes at
    # serialization time so historical rows benefit without
    # rewriting `discussions.conclusion` JSONB. Dicts are mutated in
    # place via a shallow copy to avoid leaking the enrichment back
    # onto the ORM instance (which SQLAlchemy would then dirty-track
    # and try to flush on the next commit).
    primary = enrich_conclusion_with_names(
        d.market, dict(d.conclusion) if isinstance(d.conclusion, dict) else d.conclusion,
    )
    post_mortem = enrich_conclusion_with_names(
        d.market,
        dict(d.post_mortem_conclusion)
        if isinstance(d.post_mortem_conclusion, dict)
        else d.post_mortem_conclusion,
    )
    return DiscussionResponse(
        id=d.id,
        topic=d.topic,
        rules=d.rules,
        persona_ids=list(d.persona_ids or []),
        market=d.market,
        status=d.status,
        current_round=d.current_round,
        conclusion=primary,
        post_mortem_conclusion=post_mortem,
        post_mortem_diff=d.post_mortem_diff,
        verdict=d.verdict,
        verdict_reason=d.verdict_reason,
        verified_at=d.verified_at,
        auto_run=d.auto_run,
        day1_open_prices=d.day1_open_prices,
        day5_close_prices=d.day5_close_prices,
        daily_close_prices=d.daily_close_prices,
        as_of_date=d.as_of_date.isoformat() if d.as_of_date else None,
        created_at=d.created_at,
        updated_at=d.updated_at,
    )


def _sweep_to_response(s) -> BacktestSweepResponse:
    return BacktestSweepResponse(
        id=s.id,
        status=s.status,
        topic=s.topic,
        rules=s.rules,
        market=s.market,
        persona_ids=list(s.persona_ids or []),
        anchor_date=s.anchor_date.isoformat(),
        trading_days_count=s.trading_days_count,
        rounds_per_discussion=s.rounds_per_discussion,
        concurrency=s.concurrency,
        auto_post_mortem=bool(s.auto_post_mortem),
        strategy_id=s.strategy_id,
        resolved_dates=list(s.resolved_dates or []),
        completed_dates=list(s.completed_dates or []),
        failed_dates=[
            BacktestSweepFailedDate(**fd)
            for fd in (s.failed_dates or [])
        ],
        error_message=s.error_message,
        created_at=s.created_at,
        started_at=s.started_at,
        completed_at=s.completed_at,
        cancelled_at=s.cancelled_at,
    )


def _template_to_response(t) -> StrategyTemplateResponse:
    return StrategyTemplateResponse(
        id=t.id,
        name=t.name,
        description=t.description,
        topic=t.topic,
        rules=t.rules,
        market=t.market,
        persona_ids=list(t.persona_ids or []),
        default_rounds=t.default_rounds,
        default_concurrency=t.default_concurrency,
        default_auto_post_mortem=bool(t.default_auto_post_mortem),
        persona_weights=dict(t.persona_weights or {}),
        weights_updated_at=t.weights_updated_at,
        auto_schedule_enabled=bool(t.auto_schedule_enabled),
        auto_schedule_cadence_hours=t.auto_schedule_cadence_hours,
        auto_schedule_anchor_offset_days=t.auto_schedule_anchor_offset_days,
        auto_schedule_trading_days_count=t.auto_schedule_trading_days_count,
        auto_schedule_last_run_at=t.auto_schedule_last_run_at,
        maturity_tier=getattr(t, "maturity_tier", "cold_start") or "cold_start",
        maturity_computed_at=getattr(t, "maturity_computed_at", None),
        auto_promote_enabled=bool(getattr(t, "auto_promote_enabled", False)),
        auto_promote_min_oos_brier_improvement=float(
            getattr(t, "auto_promote_min_oos_brier_improvement", 0.0) or 0.0,
        ),
        auto_promote_min_oos_hit_rate=float(
            getattr(t, "auto_promote_min_oos_hit_rate", 0.5) or 0.5,
        ),
        persona_status=dict(getattr(t, "persona_status", None) or {}),
        persona_status_updated_at=getattr(t, "persona_status_updated_at", None),
        created_at=t.created_at,
        updated_at=t.updated_at,
        deleted_at=t.deleted_at,
    )
