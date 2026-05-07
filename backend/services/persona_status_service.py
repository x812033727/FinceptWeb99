"""Per-strategy persona status governance (PR-4c).

The lifecycle treasury's last open loop: persona weights shrink
to ~0 across many sweeps but the persona still gets called every
round, polluting the synthesizer prompt with low-signal opinions.

Three states per `(strategy, persona)`:

  - `active` (default; absent from the map = active) — runs every
    round, output goes to the synthesizer.
  - `frozen` — excluded from the round roster entirely. Saves
    LLM cost + cleans up the prompt.
  - `shadow` — runs every round but the output is dropped before
    the synthesizer sees it. Placeholder for the PR-B5 shadow
    testing flow (try a new persona behind a switch).

`auto_freeze_underperformers` runs after weight learning in Phase
3 and flips a persona to `frozen` when its learned weight has
been ≤ `FREEZE_WEIGHT_THRESHOLD` across the LAST `FREEZE_LOOKBACK_
SWEEPS` production sweeps. Reads strategy_versions (PR-4a) for the
weight history. Operators unfreeze manually via the API endpoint.

The roster filter `filter_roster_by_status` is hooked into
`discussion_service._resolve_persona_specs` so frozen personas
disappear from a discussion's roster before the round starts.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.strategy_version import StrategyVersion

log = logging.getLogger(__name__)


VALID_STATUSES = ("active", "frozen", "shadow")
FREEZE_WEIGHT_THRESHOLD = 0.05
FREEZE_LOOKBACK_SWEEPS = 5


def filter_roster_by_status(
    *,
    persona_ids: list[str],
    persona_status: dict[str, str] | None,
) -> list[str]:
    """Drop frozen personas from the round roster. Shadow stays
    in the roster (run + telemetry) — its output is dropped at
    the synthesizer side, not at the round runner side. Pure
    function so the caller can use it in any code path
    (run_round, sweep worker dispatch, etc.).
    """
    status_map = persona_status or {}
    return [
        pid for pid in (persona_ids or [])
        if status_map.get(pid, "active") != "frozen"
    ]


def is_shadow_persona(
    persona_id: str,
    persona_status: dict[str, str] | None,
) -> bool:
    """Helper for the synthesizer / aggregator: is this persona's
    output supposed to be dropped before downstream consumers see
    it? Active and absent personas return False."""
    return (persona_status or {}).get(persona_id) == "shadow"


async def _recent_weight_history(
    db: AsyncSession,
    strategy_id: UUID,
    *,
    lookback: int,
) -> list[dict[str, float]]:
    """Pull the latest `lookback` weights versions in chrono
    order (newest first). Returns a list of weight dicts; missing
    rows return [] so the caller handles cold start.
    """
    rows = list((await db.scalars(
        select(StrategyVersion)
        .where(
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.artifact_kind == "weights",
        )
        .order_by(StrategyVersion.fit_at.desc())
        .limit(max(int(lookback), 1))
    )).all())
    return [dict(r.payload or {}) for r in rows]


async def auto_freeze_underperformers(
    db: AsyncSession,
    *,
    strategy_id: UUID,
) -> list[str]:
    """Flip personas to `frozen` when their weight has been
    ≤ `FREEZE_WEIGHT_THRESHOLD` across the LAST
    `FREEZE_LOOKBACK_SWEEPS` production weight versions.

    Conservative: requires the FULL lookback window to be present
    AND every value in it to fail the threshold — no premature
    freezing during the first few sweeps. Returns the list of
    persona IDs newly frozen on this pass.

    Already-frozen personas are skipped (no double-stamp on
    `persona_status_updated_at`). Already-shadow personas are
    NOT auto-frozen — operator chose shadow on purpose.
    """
    history = await _recent_weight_history(
        db, strategy_id, lookback=FREEZE_LOOKBACK_SWEEPS,
    )
    if len(history) < FREEZE_LOOKBACK_SWEEPS:
        return []

    tmpl = await db.scalar(
        select(DiscussionStrategyTemplate).where(
            DiscussionStrategyTemplate.id == strategy_id,
        )
    )
    if tmpl is None:
        return []

    status_map: dict[str, str] = dict(tmpl.persona_status or {})
    candidates: set[str] = set()
    for pid in tmpl.persona_ids or []:
        current = status_map.get(pid, "active")
        if current != "active":
            continue
        # Persona must appear in EVERY snapshot with a low weight.
        # If it's missing from any snapshot we can't make a call;
        # skip to avoid false freeze.
        ok = True
        for weights in history:
            try:
                w = float(weights.get(pid, FREEZE_WEIGHT_THRESHOLD + 1.0))
            except (TypeError, ValueError):
                ok = False
                break
            if w > FREEZE_WEIGHT_THRESHOLD:
                ok = False
                break
        if ok:
            candidates.add(pid)

    if not candidates:
        return []

    for pid in candidates:
        status_map[pid] = "frozen"
    tmpl.persona_status = status_map
    tmpl.persona_status_updated_at = datetime.now(UTC)
    await db.commit()
    return sorted(candidates)


async def set_persona_status(
    db: AsyncSession,
    *,
    owner_id: UUID,
    strategy_id: UUID,
    persona_id: str,
    status: str,
) -> dict[str, Any]:
    """Manual operator override — flip one persona's status to
    `active` / `frozen` / `shadow`. Owner-scoped + idempotent."""
    if status not in VALID_STATUSES:
        raise ValueError(
            f"status must be one of {VALID_STATUSES}, got {status!r}"
        )
    tmpl = await db.scalar(
        select(DiscussionStrategyTemplate).where(
            DiscussionStrategyTemplate.id == strategy_id,
            DiscussionStrategyTemplate.owner_id == owner_id,
        )
    )
    if tmpl is None:
        raise ValueError("strategy template not found or not owned by caller")

    status_map: dict[str, str] = dict(tmpl.persona_status or {})
    if status == "active":
        # Active is the absence of an entry — keeps the map small
        # over time as personas thaw.
        status_map.pop(persona_id, None)
    else:
        status_map[persona_id] = status
    tmpl.persona_status = status_map
    tmpl.persona_status_updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(tmpl)
    return {
        "strategy_id": str(strategy_id),
        "persona_id": persona_id,
        "status": status,
        "persona_status": dict(tmpl.persona_status or {}),
    }


__all__ = [
    "VALID_STATUSES",
    "FREEZE_WEIGHT_THRESHOLD",
    "FREEZE_LOOKBACK_SWEEPS",
    "filter_roster_by_status",
    "is_shadow_persona",
    "auto_freeze_underperformers",
    "set_persona_status",
]
