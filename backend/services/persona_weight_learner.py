"""Persona weight learner (PR-C).

Reads PR-B's strategy aggregate (per-persona hit rates) and folds
it into a `persona_weights` map persisted on the strategy
template. Future sweeps using the same template apply these
weights at synthesis time so personas with a track record of
calling dates correctly nudge the recommended_symbols toward
their picks.

Smoothing + bounds:
- Personas with `discussions_count < MIN_SAMPLES` are NOT updated
  (a single random win shouldn't double their weight).
- The mean hit rate becomes 1.0 in weight space; each persona's
  weight = (hit_rate + ALPHA) / (mean_hit_rate + ALPHA).
- Bounded to `[WEIGHT_FLOOR, WEIGHT_CEIL]` so a brief hot streak
  can't 10x a persona and lock the synthesizer into one voice.

Triggers:
- `learn_weights_for_strategy(...)` is called from the API
  POST `/strategies/{id}/learn` for manual recompute.
- The sweep worker also calls it on Phase-3 completion when the
  parent sweep has a `strategy_id` — best-effort, swallowed on
  failure so the sweep itself still succeeds.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from services import strategy_template_service as tsvc
from services import sweep_aggregate_service as agg

log = logging.getLogger(__name__)


# Tunables — small enough to expose as constants now; promote to
# RuntimeTunablesCard later if the sweep dataset grows enough that
# operators want per-deployment overrides.
MIN_SAMPLES = 3        # discussions_count threshold to participate
ALPHA = 0.1            # Laplace smoothing on hit rate
WEIGHT_FLOOR = 0.5     # min weight a persona can shrink to
WEIGHT_CEIL = 2.0      # max weight a persona can grow to


async def learn_weights_for_strategy(
    db: AsyncSession,
    *,
    owner_id: UUID,
    strategy_id: UUID,
) -> dict[str, Any]:
    """Recompute persona_weights from this strategy's aggregate
    history. Returns a status dict the API can pass straight to
    the operator UI:

      {
        "updated": True | False,
        "reason": str | None,         # set when updated=False
        "weights": {persona_id: w},   # the persisted map
        "samples": {persona_id: n},   # discussions_count per persona
      }

    Soft-deleted templates are still trainable so a purged
    strategy can be revived with up-to-date weights before
    un-soft-deleting (an admin recovery path)."""
    tmpl = await tsvc.get_template(
        db,
        template_id=strategy_id,
        owner_id=owner_id,
        include_deleted=True,
    )
    if tmpl is None:
        raise ValueError("strategy template not found or not owned by caller")

    payload = await agg.aggregate_strategy(
        db, owner_id=owner_id, strategy_id=strategy_id,
    )
    per_persona = payload.get("per_persona", [])

    eligible = [
        p for p in per_persona
        if p["discussions_count"] >= MIN_SAMPLES
        and p["hit_rate"] is not None
    ]
    samples = {p["persona_id"]: p["discussions_count"] for p in per_persona}

    if not eligible:
        return {
            "updated": False,
            "reason": (
                f"no persona has reached MIN_SAMPLES={MIN_SAMPLES} "
                "with a verifiable verdict yet"
            ),
            "weights": dict(tmpl.persona_weights or {}),
            "samples": samples,
        }

    weights = compute_weights_from_aggregate(eligible)
    await tsvc.set_persona_weights(db, tmpl, weights=weights)
    return {
        "updated": True,
        "reason": None,
        "weights": weights,
        "samples": samples,
    }


def compute_weights_from_aggregate(
    eligible: list[dict[str, Any]],
) -> dict[str, float]:
    """Pure function so tests can drive it without a DB.

    Input rows are PR-B's per_persona shape with `hit_rate` and
    `discussions_count` already computed."""
    smoothed = [(p["persona_id"], p["hit_rate"] + ALPHA) for p in eligible]
    mean = sum(s for _, s in smoothed) / len(smoothed)
    if mean == 0:
        # Pathological: every eligible persona hit_rate=-ALPHA, which
        # can't happen because hit_rate is in [0, 1]. Defence in depth.
        return {pid: 1.0 for pid, _ in smoothed}

    out: dict[str, float] = {}
    for pid, s in smoothed:
        w = s / mean
        out[pid] = round(_clamp(w, WEIGHT_FLOOR, WEIGHT_CEIL), 4)
    return out


def format_weights_for_synthesizer(
    weights: dict[str, float],
) -> str:
    """Render the weights map as a synthesizer-prompt section.

    Returns "" when the map is empty, so the caller can do a
    plain `prompt += format_weights_for_synthesizer(...)` without
    branching. The synthesizer is told to use weights as a
    tie-breaker — not as an override — because a low-weighted
    persona can still be right about a specific topic."""
    if not weights:
        return ""

    sorted_w = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
    lines = "\n".join(
        f"- `{pid}`: 權重 {w:.2f}"
        for pid, w in sorted_w
    )
    return (
        "\n\n## 補充提示（Persona 歷史命中加權）\n"
        "以下權重來自過去使用同一策略模板的回測命中率（PR-C 學習）：\n"
        f"{lines}\n"
        "**用法**：當不同 persona 的觀點衝突時，**優先採用權重較高者的判斷**；"
        "權重相近視為平手。權重不取代具體論證，僅作為平手時的破局工具。"
    )


def _clamp(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


__all__ = [
    "MIN_SAMPLES", "ALPHA", "WEIGHT_FLOOR", "WEIGHT_CEIL",
    "learn_weights_for_strategy",
    "compute_weights_from_aggregate",
    "format_weights_for_synthesizer",
]
