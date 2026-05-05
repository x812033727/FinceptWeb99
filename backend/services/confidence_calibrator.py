"""Isotonic confidence calibration per strategy template (PR-C2).

PR-C0 lets the synthesizer emit a per-pick confidence; PR-C1 grades
each one against the D1-D5 outcome and persists the (raw_conf,
outcome_binary) pair on the parent discussion. This module fits
an isotonic regression over the rolling pool of pairs for one
strategy and persists the resulting curve so future synthesizer
outputs can be calibrated with one lookup.

Why isotonic over Platt:
  - LLM-emitted confidence isn't a logistic regression output —
    its distribution has fat clumps at 0.7-0.9 (system-1 over-
    confidence) plus a long tail of 0.0/1.0 emissions when the
    LLM hits a hard prior. Platt's logit assumption maps that
    poorly.
  - Isotonic only requires monotonicity (more raw → more real),
    which is the only property we actually need to preserve. It
    handles the LLM's lumpy emission pattern by collapsing
    adjacent violating points into a single bucket.

In-house PAV (Pool Adjacent Violators) keeps the dependency
surface minimal — no sklearn pull-in, ~30 lines for the core
algorithm. The trade-off is we lose sklearn's
out-of-bounds extrapolation hooks, which we don't need anyway:
raw confidences are in [0, 1] and the curve covers that range.

Sample-size gate `MIN_SAMPLES_FOR_FIT` (default 30) keeps the
curve from chasing noise during the strategy's first few sweeps.
Below that threshold, `fit_isotonic_for_strategy` returns
`updated=False` and the synthesizer keeps emitting raw confidence
until the pool grows.

Persisted shape on `discussion_strategy_templates`:

  calibration_curve = [
    {"raw": 0.30, "calibrated": 0.18},
    {"raw": 0.50, "calibrated": 0.35},
    {"raw": 0.85, "calibrated": 0.60},
    ...
  ]   # ascending by raw, monotone non-decreasing in calibrated.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion import Discussion
from models.discussion_strategy_template import DiscussionStrategyTemplate

log = logging.getLogger(__name__)


MIN_SAMPLES_FOR_FIT = 30


def fit_isotonic_pav(
    pairs: list[tuple[float, int]],
) -> list[tuple[float, float]]:
    """Pool Adjacent Violators on `(raw_confidence, outcome_binary)`
    pairs. Returns a sorted ascending list of `(x_threshold, y)`
    control points where y is monotone non-decreasing in x.

    Multiple pairs at the same x are collapsed into a single block
    upfront (their y values get averaged) so the algorithm doesn't
    emit a one-to-many curve.
    """
    if not pairs:
        return []

    grouped: dict[float, list[float]] = defaultdict(list)
    for x, y in pairs:
        if y not in (0, 1):
            continue
        grouped[float(x)].append(float(y))
    if not grouped:
        return []

    blocks: list[tuple[float, int, float]] = []   # (sum_y, count, x)
    for x in sorted(grouped.keys()):
        ys = grouped[x]
        blocks.append((sum(ys), len(ys), x))

    stack: list[tuple[float, int, float]] = []
    for sum_y, count, x in blocks:
        new_sum = sum_y
        new_count = count
        new_x = x
        while stack and (
            stack[-1][0] / stack[-1][1] > new_sum / new_count
        ):
            top_sum, top_count, _ = stack.pop()
            new_sum += top_sum
            new_count += top_count
            # x stays the right-most (largest) — the block now
            # represents [previous_x_lower, new_x] and we use the
            # upper bound for lookup.
        stack.append((new_sum, new_count, new_x))

    return [(b[2], round(b[0] / b[1], 6)) for b in stack]


def apply_calibration(
    raw_confidence: float,
    curve: list[dict[str, float] | tuple[float, float]] | None,
) -> float:
    """Map a raw synthesizer confidence to its calibrated estimate.

    `curve` is the persisted JSON shape (list of dicts) OR the
    in-memory tuple shape `fit_isotonic_pav` emits — accept both
    so the function works at fit time without a serialize-then-
    re-parse round trip. Empty / missing curve returns the raw
    value unchanged.
    """
    if not curve:
        return max(0.0, min(1.0, float(raw_confidence)))

    rc = max(0.0, min(1.0, float(raw_confidence)))
    points: list[tuple[float, float]] = []
    for entry in curve:
        if isinstance(entry, dict):
            try:
                points.append(
                    (float(entry["raw"]), float(entry["calibrated"]))
                )
            except (KeyError, TypeError, ValueError):
                continue
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            try:
                points.append((float(entry[0]), float(entry[1])))
            except (TypeError, ValueError):
                continue
    if not points:
        return rc
    points.sort(key=lambda p: p[0])

    for x_thr, y_val in points:
        if rc <= x_thr:
            return max(0.0, min(1.0, y_val))
    # raw exceeds the highest observed bucket — return the
    # rightmost calibrated value rather than extrapolating.
    return max(0.0, min(1.0, points[-1][1]))


async def _gather_calibration_pairs(
    db: AsyncSession,
    *,
    owner_id: UUID,
    strategy_id: UUID,
) -> list[tuple[float, int]]:
    """Walk every concluded child discussion of every sweep that
    references `strategy_id` and pull `(confidence, outcome_binary)`
    pairs out of `outcome_vector`.

    Pre-PR-C0 discussions don't have outcome_vector so they're
    silently skipped. Owner-scoped at the sweep level — soft-
    deleted templates still aggregate so a recovery flow can
    re-fit before un-deleting.
    """
    sweeps_q = select(BacktestSweep.id).where(
        BacktestSweep.owner_id == owner_id,
        BacktestSweep.strategy_id == strategy_id,
    )
    sweep_ids = list((await db.scalars(sweeps_q)).all())
    if not sweep_ids:
        return []

    disc_q = (
        select(Discussion.outcome_vector)
        .where(
            Discussion.sweep_id.in_(sweep_ids),
            Discussion.outcome_vector.is_not(None),
        )
    )
    rows = list((await db.scalars(disc_q)).all())
    pairs: list[tuple[float, int]] = []
    for vec in rows:
        if not isinstance(vec, list):
            continue
        for entry in vec:
            if not isinstance(entry, dict):
                continue
            try:
                conf = float(entry.get("confidence"))
                outcome = int(entry.get("outcome_binary"))
            except (TypeError, ValueError):
                continue
            if outcome not in (0, 1):
                continue
            conf = max(0.0, min(1.0, conf))
            pairs.append((conf, outcome))
    return pairs


async def fit_isotonic_for_strategy(
    db: AsyncSession,
    *,
    owner_id: UUID,
    strategy_id: UUID,
) -> dict[str, Any]:
    """Recompute the isotonic curve from this strategy's pool of
    (confidence, outcome) pairs and persist it on the template.

    Returns a status dict the API / sweep worker can pass straight
    to telemetry:

      {
        "updated": True | False,
        "reason": str | None,         # set when updated=False
        "curve": [{"raw": .., "calibrated": ..}, ...],
        "samples": int,
      }

    Soft-deleted templates are still fittable so a purged
    strategy can be revived with up-to-date calibration before
    un-soft-deleting (mirrors persona_weight_learner)."""
    tmpl = await db.scalar(
        select(DiscussionStrategyTemplate).where(
            DiscussionStrategyTemplate.id == strategy_id,
            DiscussionStrategyTemplate.owner_id == owner_id,
        )
    )
    if tmpl is None:
        raise ValueError(
            "strategy template not found or not owned by caller"
        )

    pairs = await _gather_calibration_pairs(
        db, owner_id=owner_id, strategy_id=strategy_id,
    )
    if len(pairs) < MIN_SAMPLES_FOR_FIT:
        return {
            "updated": False,
            "reason": (
                f"need {MIN_SAMPLES_FOR_FIT} samples to fit, "
                f"got {len(pairs)}"
            ),
            "curve": list(tmpl.calibration_curve or []),
            "samples": len(pairs),
        }

    raw_curve = fit_isotonic_pav(pairs)
    serialized = [{"raw": x, "calibrated": y} for x, y in raw_curve]
    now = datetime.now(UTC)

    tmpl.calibration_curve = serialized
    tmpl.calibration_updated_at = now
    tmpl.calibration_sample_count = len(pairs)
    await db.commit()
    await db.refresh(tmpl)

    return {
        "updated": True,
        "reason": None,
        "curve": serialized,
        "samples": len(pairs),
    }


__all__ = [
    "MIN_SAMPLES_FOR_FIT",
    "fit_isotonic_pav",
    "apply_calibration",
    "fit_isotonic_for_strategy",
]
