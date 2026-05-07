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

# PR-3: safety thresholds for the deployment gate. Tuned conservatively;
# operators can override by calling `evaluate_curve_safety` with an
# explicit `thresholds` dict (e.g. tightening for high-stakes
# strategies). The defaults are picked so a typical month-on-month
# refit flows through unchanged but a sudden regime-driven shift
# parks for review.
SAFETY_MAX_CONTROL_POINT_DELTA = 0.30   # any one raw bin shifts by > 0.3 calibrated → review
SAFETY_MIN_ENDPOINT_GAP = -0.05         # curve(0.95) - curve(0.5) < -0.05 → endpoint_inversion
SAFETY_BRIER_DEGRADATION_RATIO = 1.30   # recent_brier / baseline_brier > 1.3 → degradation flag


def evaluate_curve_safety(
    old_curve: list[dict[str, float] | tuple[float, float]] | None,
    new_curve: list[dict[str, float] | tuple[float, float]] | None,
    *,
    recent_brier: float | None = None,
    baseline_brier: float | None = None,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """PR-3: assess whether a freshly-fitted curve is safe to deploy.

    Returns:

      {
        "safe": bool,
        "reason": str | None,        # human-readable summary when not safe
        "monotonic": bool,           # PAV should always emit this; defensive check
        "max_control_point_delta": float,
        "endpoint_gap": float,       # curve(highest x) - curve(midpoint x); negative = inversion risk
        "brier_degradation_ratio": float | None,
      }

    Three guards:

      1. **Monotonicity** — `fit_isotonic_pav` should always emit a
         non-decreasing curve, but we verify defensively in case a
         downstream merge or partial write corrupted the JSON.
      2. **Max control-point delta** — comparing the new curve at each
         of its raw bins against the old curve's prediction. If any
         bin shifts by more than `SAFETY_MAX_CONTROL_POINT_DELTA`
         (default 0.3), the new curve is structurally different
         enough to warrant manual review (typical regime change
         signal).
      3. **Brier degradation** — when the caller supplies recent vs
         baseline Brier, ratio > `SAFETY_BRIER_DEGRADATION_RATIO`
         (default 1.3) flags the new curve as worse than the live
         one over the recent window.

    `old_curve` may be empty / None on cold-start (first fit ever);
    in that case the delta check is skipped and the curve only has
    to pass monotonicity + (optional) brier check.
    """
    th = thresholds or {}
    max_delta_threshold = float(
        th.get("max_control_point_delta", SAFETY_MAX_CONTROL_POINT_DELTA)
    )
    endpoint_gap_min = float(
        th.get("min_endpoint_gap", SAFETY_MIN_ENDPOINT_GAP)
    )
    brier_ratio_threshold = float(
        th.get("brier_degradation_ratio", SAFETY_BRIER_DEGRADATION_RATIO)
    )

    new_points = _curve_to_tuples(new_curve)

    monotonic = all(
        new_points[i][1] <= new_points[i + 1][1] + 1e-9
        for i in range(len(new_points) - 1)
    ) if new_points else True

    endpoint_gap = 0.0
    if len(new_points) >= 2:
        # Compare highest-x calibrated to the closest-to-midpoint
        # calibrated. A modest dip is fine; > endpoint_gap_min worth
        # of inversion is suspicious.
        midpoint_x = 0.5
        mid_y = apply_calibration(midpoint_x, new_curve)
        top_y = new_points[-1][1]
        endpoint_gap = round(top_y - mid_y, 6)

    max_delta = 0.0
    if old_curve and new_points:
        for raw_x, new_y in new_points:
            old_y = apply_calibration(raw_x, old_curve)
            delta = abs(new_y - old_y)
            if delta > max_delta:
                max_delta = delta

    brier_ratio: float | None = None
    if (
        recent_brier is not None
        and baseline_brier is not None
        and baseline_brier > 0.0
    ):
        brier_ratio = round(recent_brier / baseline_brier, 4)

    reasons: list[str] = []
    if not monotonic:
        reasons.append("non-monotone curve")
    if endpoint_gap < endpoint_gap_min:
        reasons.append(
            f"endpoint inversion (top vs mid Δ={endpoint_gap:+.3f})"
        )
    if max_delta > max_delta_threshold:
        reasons.append(
            f"large shift vs prior curve (max Δ={max_delta:.3f} > "
            f"{max_delta_threshold:.2f})"
        )
    if brier_ratio is not None and brier_ratio > brier_ratio_threshold:
        reasons.append(
            f"brier degradation (recent/baseline={brier_ratio:.2f} > "
            f"{brier_ratio_threshold:.2f})"
        )

    safe = not reasons
    return {
        "safe": safe,
        "reason": "; ".join(reasons) if reasons else None,
        "monotonic": monotonic,
        "max_control_point_delta": round(max_delta, 6),
        "endpoint_gap": endpoint_gap,
        "brier_degradation_ratio": brier_ratio,
    }


def _curve_to_tuples(
    curve: list[dict[str, float] | tuple[float, float]] | None,
) -> list[tuple[float, float]]:
    """Normalise either persisted (list[dict]) or in-memory (list[tuple])
    curve format to sorted (raw, calibrated) tuples. Drops malformed
    entries silently — a half-corrupted curve still gives the safety
    check the well-formed bits to reason over."""
    if not curve:
        return []
    out: list[tuple[float, float]] = []
    for entry in curve:
        if isinstance(entry, dict):
            try:
                out.append(
                    (float(entry["raw"]), float(entry["calibrated"]))
                )
            except (KeyError, TypeError, ValueError):
                continue
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            try:
                out.append((float(entry[0]), float(entry[1])))
            except (TypeError, ValueError):
                continue
    out.sort(key=lambda p: p[0])
    return out


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


async def _compute_recent_brier_signals(
    db: AsyncSession,
    *,
    strategy_id: UUID,
    sample_count_recent: int = 30,
) -> tuple[float | None, float | None]:
    """PR-3: rough recent-vs-baseline brier for the deployment gate.

    Pulls every concluded discussion for this strategy in
    chronological order and splits into "recent N" vs "everything
    older". Mean brier on each half. Returns (None, None) when
    there isn't enough data on either side — the safety check then
    just skips the brier guard.

    Cheap aggregation: uses the persisted `discussion.brier_score`
    column (PR-C1) so no per-pick math here.
    """
    sweeps_q = select(BacktestSweep.id).where(
        BacktestSweep.strategy_id == strategy_id,
    )
    sweep_ids = list((await db.scalars(sweeps_q)).all())
    if not sweep_ids:
        return (None, None)

    rows = list((await db.scalars(
        select(Discussion.brier_score)
        .where(
            Discussion.sweep_id.in_(sweep_ids),
            Discussion.brier_score.is_not(None),
        )
        .order_by(Discussion.created_at.asc())
    )).all())
    if len(rows) < sample_count_recent + 5:
        # Not enough to meaningfully split — skip the brier guard.
        return (None, None)
    recent = [float(r) for r in rows[-sample_count_recent:]]
    baseline = [float(r) for r in rows[:-sample_count_recent]]
    if not recent or not baseline:
        return (None, None)
    return (sum(recent) / len(recent), sum(baseline) / len(baseline))


async def fit_isotonic_for_strategy(
    db: AsyncSession,
    *,
    owner_id: UUID,
    strategy_id: UUID,
    auto_deploy: bool = True,
) -> dict[str, Any]:
    """Recompute the isotonic curve from this strategy's pool of
    (confidence, outcome) pairs and persist it on the template.

    PR-3 deployment gate (`auto_deploy=True`, default):
      1. Fit the new curve from the rolling pool.
      2. Run `evaluate_curve_safety` against the LIVE curve +
         recent vs baseline brier.
      3. If safe → write to `calibration_curve` (preserves the
         existing PR-C2 contract).
      4. If unsafe → write to `calibration_pending_curve` +
         `calibration_pending_reason` + `calibration_pending_at`
         (live curve untouched). Admin approves via
         `approve_pending_calibration` to promote.

    Pass `auto_deploy=False` to bypass the gate entirely (operator
    workflow for manual recalibration that always lands as pending).

    Returns a status dict the API / sweep worker can pass straight
    to telemetry:

      {
        "status": "deployed" | "pending_review" | "no_change",
        "updated": bool,             # alias for backwards-compat (deployed → True)
        "reason": str | None,        # gate failure summary OR sample-size message
        "curve": [...],
        "samples": int,
        "safety": dict,              # full evaluate_curve_safety output (None if no fit)
      }

    Soft-deleted templates are still fittable so a purged
    strategy can be revived with up-to-date calibration before
    un-soft-deleting (mirrors persona_weight_learner).
    """
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
            "status": "no_change",
            "updated": False,
            "reason": (
                f"need {MIN_SAMPLES_FOR_FIT} samples to fit, "
                f"got {len(pairs)}"
            ),
            "curve": list(tmpl.calibration_curve or []),
            "samples": len(pairs),
            "safety": None,
        }

    raw_curve = fit_isotonic_pav(pairs)
    serialized = [{"raw": x, "calibrated": y} for x, y in raw_curve]
    now = datetime.now(UTC)

    recent_brier, baseline_brier = await _compute_recent_brier_signals(
        db, strategy_id=strategy_id,
    )
    safety = evaluate_curve_safety(
        old_curve=tmpl.calibration_curve,
        new_curve=serialized,
        recent_brier=recent_brier,
        baseline_brier=baseline_brier,
    )

    if safety["safe"] or not auto_deploy:
        # auto_deploy=False ALWAYS lands as pending so an operator
        # invoking the manual refit can review before promoting.
        # When safe AND auto_deploy=True, deploy directly.
        if not auto_deploy:
            tmpl.calibration_pending_curve = serialized
            tmpl.calibration_pending_reason = (
                "manual fit (auto_deploy=False); awaiting approval"
            )
            tmpl.calibration_pending_at = now
            await db.commit()
            await db.refresh(tmpl)
            return {
                "status": "pending_review",
                "updated": False,
                "reason": "manual fit queued for review",
                "curve": serialized,
                "samples": len(pairs),
                "safety": safety,
            }
        tmpl.calibration_curve = serialized
        tmpl.calibration_updated_at = now
        tmpl.calibration_sample_count = len(pairs)
        # Clear any previously-pending curve once a new safe fit
        # deploys cleanly — operator no longer needs to act on the
        # old pending one.
        tmpl.calibration_pending_curve = None
        tmpl.calibration_pending_reason = None
        tmpl.calibration_pending_at = None
        await db.commit()
        await db.refresh(tmpl)
        # PR-4a: capture this fit in the version history. Failure
        # logged but doesn't reverse the live deploy — the curve
        # is already serving predictions and rolling that back over
        # an audit-trail gap would do more harm than good.
        try:
            from services import strategy_version_service
            await strategy_version_service.record_version(
                db,
                strategy_id=strategy_id,
                artifact_kind="calibration_curve",
                payload=serialized,
                sample_count=len(pairs),
            )
        except Exception as exc:
            log.warning(
                "confidence_calibrator.version_record_failed",
                extra={
                    "strategy_id": str(strategy_id),
                    "error": str(exc),
                },
            )
        return {
            "status": "deployed",
            "updated": True,
            "reason": None,
            "curve": serialized,
            "samples": len(pairs),
            "safety": safety,
        }

    # Unsafe + auto_deploy=True → park for review without touching
    # the live curve. The previous pending (if any) gets overwritten
    # — only one queued review per strategy.
    tmpl.calibration_pending_curve = serialized
    tmpl.calibration_pending_reason = safety["reason"]
    tmpl.calibration_pending_at = now
    await db.commit()
    await db.refresh(tmpl)
    return {
        "status": "pending_review",
        "updated": False,
        "reason": safety["reason"],
        "curve": serialized,
        "samples": len(pairs),
        "safety": safety,
    }


async def approve_pending_calibration(
    db: AsyncSession,
    *,
    owner_id: UUID,
    strategy_id: UUID,
) -> dict[str, Any]:
    """PR-3: promote the pending calibration curve to the live one.

    Owner-scoped (admin / strategy owner). Idempotent: calling on a
    template with no pending curve returns `{"promoted": False}`
    without touching state.

    Returns:
      {
        "promoted": bool,
        "curve": [...] | None,    # the now-live curve (or current live if not promoted)
        "reason": str | None,     # the queued reason that the operator just overrode
      }
    """
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

    if not tmpl.calibration_pending_curve:
        return {
            "promoted": False,
            "curve": list(tmpl.calibration_curve or []),
            "reason": None,
        }

    pending = list(tmpl.calibration_pending_curve)
    queued_reason = tmpl.calibration_pending_reason
    sample_count_at_promote = tmpl.calibration_sample_count
    tmpl.calibration_curve = pending
    tmpl.calibration_updated_at = datetime.now(UTC)
    tmpl.calibration_pending_curve = None
    tmpl.calibration_pending_reason = None
    tmpl.calibration_pending_at = None
    await db.commit()
    await db.refresh(tmpl)
    # PR-4a: an approve also deploys, so it joins the version
    # history. Reason carries the gate's original failure summary
    # so the audit shows "why this needed manual review."
    try:
        from services import strategy_version_service
        await strategy_version_service.record_version(
            db,
            strategy_id=strategy_id,
            artifact_kind="calibration_curve",
            payload=pending,
            sample_count=sample_count_at_promote,
            notes=(
                f"approved from pending; original reason: {queued_reason}"
                if queued_reason else "approved from pending"
            ),
        )
    except Exception as exc:
        log.warning(
            "confidence_calibrator.approve_version_record_failed",
            extra={
                "strategy_id": str(strategy_id),
                "error": str(exc),
            },
        )
    return {
        "promoted": True,
        "curve": pending,
        "reason": queued_reason,
    }


async def reject_pending_calibration(
    db: AsyncSession,
    *,
    owner_id: UUID,
    strategy_id: UUID,
) -> dict[str, Any]:
    """PR-3: discard the pending calibration curve and keep serving
    the live one. Idempotent on no-pending."""
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
    if not tmpl.calibration_pending_curve:
        return {"rejected": False, "had_pending": False}
    tmpl.calibration_pending_curve = None
    tmpl.calibration_pending_reason = None
    tmpl.calibration_pending_at = None
    await db.commit()
    return {"rejected": True, "had_pending": True}


__all__ = [
    "MIN_SAMPLES_FOR_FIT",
    "SAFETY_MAX_CONTROL_POINT_DELTA",
    "SAFETY_MIN_ENDPOINT_GAP",
    "SAFETY_BRIER_DEGRADATION_RATIO",
    "fit_isotonic_pav",
    "apply_calibration",
    "evaluate_curve_safety",
    "fit_isotonic_for_strategy",
    "approve_pending_calibration",
    "reject_pending_calibration",
]
