"""Discussion strategy-template sub-router.

Owns the strategy lifecycle surface: template CRUD, calibration
deployment gate (PR-3), version history + maturity tier (PR-4a),
persona status overrides (PR-4c), regime overlay + cross-strategy
comparison (PR-5c), persona leaderboard (PR-5b), health metrics +
auto-promote thresholds (PR-4b), per-template aggregate / weight-
learner / Brier history (PR-C), soft-delete, and the walk-forward
trigger (PR-A1).

Mounted under `/api/discussion` so paths keep the `/api/discussion/...`
shape the frontend already calls.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.discussion._helpers import (
    CurrentUser,
    _coerce_owner_uuid,
    _template_to_response,
)
from api.discussion.schemas import (
    PersonaWeightLearnResponse,
    StrategyBrierHistoryPoint,
    StrategyTemplateCreate,
    StrategyTemplateResponse,
    StrategyTemplateUpdate,
    SweepAggregateResponse,
    WalkForwardFoldOut,
    WalkForwardPlanResponse,
    WalkForwardRequest,
)
from db.session import get_db

router = APIRouter()


# ── Strategy templates (PR-A) ────────────────────────────────────


@router.get(
    "/strategies",
    response_model=list[StrategyTemplateResponse],
)
async def list_strategies(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Most-recent-first list of the caller's strategy templates."""
    from services import strategy_template_service as tsvc
    rows = await tsvc.list_templates(
        db, owner_id=_coerce_owner_uuid(user),
    )
    return [_template_to_response(r) for r in rows]


@router.post(
    "/strategies",
    response_model=StrategyTemplateResponse,
    status_code=201,
)
async def create_strategy(
    body: StrategyTemplateCreate,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from services import strategy_template_service as tsvc
    try:
        tmpl = await tsvc.create_template(
            db, owner_id=_coerce_owner_uuid(user),
            name=body.name,
            description=body.description,
            topic=body.topic,
            rules=body.rules,
            market=body.market,
            persona_ids=body.persona_ids,
            default_rounds=body.default_rounds,
            default_concurrency=body.default_concurrency,
            default_auto_post_mortem=body.default_auto_post_mortem,
            auto_schedule_enabled=body.auto_schedule_enabled,
            auto_schedule_cadence_hours=body.auto_schedule_cadence_hours,
            auto_schedule_anchor_offset_days=body.auto_schedule_anchor_offset_days,
            auto_schedule_trading_days_count=body.auto_schedule_trading_days_count,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _template_to_response(tmpl)


@router.get(
    "/strategies/{template_id}",
    response_model=StrategyTemplateResponse,
)
async def get_strategy(
    template_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from services import strategy_template_service as tsvc
    row = await tsvc.get_template(
        db, template_id=template_id,
        owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return _template_to_response(row)


@router.patch(
    "/strategies/{template_id}",
    response_model=StrategyTemplateResponse,
)
async def update_strategy(
    template_id: uuid.UUID,
    body: StrategyTemplateUpdate,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from services import strategy_template_service as tsvc
    row = await tsvc.get_template(
        db, template_id=template_id,
        owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    patch = body.model_dump(exclude_unset=True)
    try:
        updated = await tsvc.update_template(db, row, patch=patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _template_to_response(updated)


# ── PR-3: calibration deployment gate ─────────────────────────────


@router.get("/strategies/{template_id}/calibration/pending")
async def get_pending_calibration(
    template_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PR-3: surface the queued (not-yet-deployed) calibration curve
    for an admin review card. Returns the live curve alongside so
    the UI can render a side-by-side diff.

    `pending_curve` is null when no review is queued (the typical
    case). The frontend uses this to gate "Approve" / "Reject"
    button visibility.
    """
    from services import strategy_template_service as tsvc
    row = await tsvc.get_template(
        db, template_id=template_id,
        owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return {
        "strategy_id": str(row.id),
        "live_curve": row.calibration_curve or [],
        "live_updated_at": (
            row.calibration_updated_at.isoformat()
            if row.calibration_updated_at else None
        ),
        "live_sample_count": row.calibration_sample_count,
        "pending_curve": row.calibration_pending_curve,
        "pending_reason": row.calibration_pending_reason,
        "pending_at": (
            row.calibration_pending_at.isoformat()
            if row.calibration_pending_at else None
        ),
    }


@router.post("/strategies/{template_id}/calibration/approve")
async def approve_calibration_route(
    template_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PR-3: promote a queued calibration curve to the live one.
    Owner-scoped; idempotent (no pending → returns `{promoted: false}`
    without touching state)."""
    from services import confidence_calibrator
    try:
        result = await confidence_calibrator.approve_pending_calibration(
            db,
            owner_id=_coerce_owner_uuid(user),
            strategy_id=template_id,
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return result


@router.post("/strategies/{template_id}/calibration/reject")
async def reject_calibration_route(
    template_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PR-3: discard the queued curve, keep serving the live one.
    Idempotent on no-pending."""
    from services import confidence_calibrator
    try:
        result = await confidence_calibrator.reject_pending_calibration(
            db,
            owner_id=_coerce_owner_uuid(user),
            strategy_id=template_id,
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return result


# ── PR-4a: version history + maturity tier ────────────────────────


@router.get("/strategies/{template_id}/versions")
async def list_strategy_versions(
    template_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    artifact_kind: str | None = None,
    limit: int = 20,
):
    """PR-4a: newest-first version history for the operator's
    rollback drawer. `artifact_kind` filters to weights or curve;
    omit to interleave both chronologically."""
    from services import strategy_template_service as tsvc
    from services import strategy_version_service as vsvc
    row = await tsvc.get_template(
        db, template_id=template_id,
        owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    try:
        versions = await vsvc.list_versions(
            db,
            strategy_id=template_id,
            artifact_kind=artifact_kind,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return [vsvc.version_to_dict(v) for v in versions]


@router.post(
    "/strategies/{template_id}/versions/{version_id}/rollback",
)
async def rollback_strategy_version(
    template_id: uuid.UUID,
    version_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PR-4a: promote a historical version back to live + audit
    the rollback as a NEW version row (so the rollback itself
    appears in the history). Owner-scoped."""
    from services import strategy_version_service as vsvc
    try:
        result = await vsvc.rollback_to_version(
            db,
            owner_id=_coerce_owner_uuid(user),
            strategy_id=template_id,
            version_id=version_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return result


@router.get("/strategies/{template_id}/maturity")
async def get_strategy_maturity(
    template_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PR-4a: lifecycle tier + the signals that drove the
    classification. Persisted on the template; this endpoint
    returns the latest persisted value, fresh-computing only when
    the cached value is missing (cold-start row).
    """
    from services import strategy_maturity_service as msvc
    from services import strategy_template_service as tsvc
    row = await tsvc.get_template(
        db, template_id=template_id,
        owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if row.maturity_computed_at is None:
        # Cold-start path — compute on first read so the UI never
        # gets a stale 'cold_start' for a row that's actually past it.
        tier, signals = await msvc.update_maturity_tier(
            db, strategy_id=template_id,
        )
    else:
        tier, signals = await msvc.compute_maturity_tier(
            db, strategy_id=template_id,
        )
    return {
        "strategy_id": str(template_id),
        "tier": tier,
        "signals": signals,
        "computed_at": (
            row.maturity_computed_at.isoformat()
            if row.maturity_computed_at else None
        ),
    }


@router.get("/strategies/{template_id}/timeline")
async def get_strategy_timeline(
    template_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = 90,
):
    """PR-5a: assembled lifecycle timeline (metrics + events) for
    the strategy. Owner-scoped.

    Returns one chronological payload combining:
      - rolling-30 brier / hit-rate / sample series from
        strategy_health_metrics
      - version-change events from strategy_versions (each fit's
        trigger inferred from notes: auto_promote / rollback /
        sweep_phase3 / etc.)
      - sweep-completed events from backtest_sweeps
      - maturity-tier transitions inferred from consecutive
        snapshot rows
      - persona-status updates from the strategy's
        persona_status_updated_at stamp

    `days` clamps every source to the trailing window. Empty
    arrays everywhere on cold-start strategies.
    """
    from services import strategy_template_service as tsvc
    from services import strategy_timeline_service as tlsvc
    row = await tsvc.get_template(
        db, template_id=template_id,
        owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return await tlsvc.compose_timeline(
        db, strategy_id=template_id, days=days,
    )


# ── PR-4c: persona status + lesson archive ────────────────────────


@router.patch(
    "/strategies/{template_id}/personas/{persona_id}/status",
)
async def patch_persona_status(
    template_id: uuid.UUID,
    persona_id: str,
    body: dict,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PR-4c: manually flip a persona's status (active/frozen/
    shadow) for a strategy. Owner-scoped. The body shape is
    `{"status": "active"|"frozen"|"shadow"}`.

    Operators use this to:
      - thaw a persona that the auto-freeze gate caught wrongly
      - manually freeze a persona before the gate gets there
      - flip a persona to shadow mode for A/B-style testing
        without removing it from the roster entirely
    """
    raw_status = str(body.get("status", "")).strip()
    from services import persona_status_service
    if raw_status not in persona_status_service.VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"status must be one of "
                f"{persona_status_service.VALID_STATUSES}"
            ),
        )
    try:
        result = await persona_status_service.set_persona_status(
            db,
            owner_id=_coerce_owner_uuid(user),
            strategy_id=template_id,
            persona_id=persona_id,
            status=raw_status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return result


# ── PR-5c: regime overlay + strategy comparison ──────────────────


@router.get("/regime-bands")
async def get_regime_bands(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    market: str = "TW",
    days: int = 90,
):
    """PR-5c: market regime bands for the timeline overlay.

    Currently implemented for TW only (uses `_TAIEX` from
    `ohlcv_daily` + `tw_vix_daily`); US returns [] until SPX +
    ^VIX get persisted.

    Returns `[{start, end, regime}, ...]` where regimes can
    overlap (a bull-run day with high vol shows up in BOTH the
    bull AND high_vol band lists).
    """
    from datetime import datetime, timedelta, UTC
    from services import regime_classifier
    days = max(min(int(days), 365), 1)
    end = datetime.now(UTC).date()
    start = end - timedelta(days=days)
    bands = await regime_classifier.classify_regimes(
        db, market=market, start=start, end=end,
    )
    return {
        "market": market,
        "days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bands": bands,
    }


@router.post("/strategies/compare")
async def post_strategies_compare(
    body: dict,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PR-5c: cross-strategy comparison for the StrategyComparisonPage.
    Body: `{"strategy_ids": [...], "days": 90}`.

    Up to 4 strategies; excess are truncated. Owner-scoped — IDs
    not owned by the caller are silently dropped (the response
    array might be shorter than the request).
    """
    from services import strategy_comparison_service
    raw_ids = body.get("strategy_ids") or []
    days = int(body.get("days", 90))
    parsed_ids: list[uuid.UUID] = []
    for raw in raw_ids:
        try:
            parsed_ids.append(uuid.UUID(str(raw)))
        except (TypeError, ValueError):
            continue
    payload = await strategy_comparison_service.compare_strategies(
        db,
        owner_id=_coerce_owner_uuid(user),
        strategy_ids=parsed_ids,
        days=days,
    )
    return payload


@router.get(
    "/strategies/{template_id}/versions/{v1_id}/compare/{v2_id}",
)
async def compare_strategy_versions(
    template_id: uuid.UUID,
    v1_id: uuid.UUID,
    v2_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PR-5c: diff two version rows of the same strategy.
    Both must share the same artifact_kind."""
    from services import strategy_comparison_service
    try:
        payload = await strategy_comparison_service.compare_versions(
            db,
            owner_id=_coerce_owner_uuid(user),
            strategy_id=template_id,
            v1_id=v1_id,
            v2_id=v2_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return payload


# ── PR-5b: persona leaderboard + lesson library ──────────────────


@router.get("/personas/leaderboard")
async def get_persona_leaderboard(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    strategy_id: uuid.UUID | None = None,
    market: str | None = None,
    days: int = 90,
):
    """PR-5b: per-persona performance roll-up for the leaderboard
    UI. Owner-scoped. When `strategy_id` is supplied, the response
    rows include `average_weight` + `weight_trend_30d` (only
    meaningful per strategy); without it those fields are NULL.

    Sorted by win-attribution rate desc → participation count
    desc, so consistent contributors surface first.
    """
    from services import persona_performance_service as svc
    rows = await svc.compute_persona_performance(
        db,
        owner_id=_coerce_owner_uuid(user),
        strategy_id=strategy_id,
        market=market,
        days=days,
    )
    return {
        "owner_id": str(_coerce_owner_uuid(user)),
        "strategy_id": str(strategy_id) if strategy_id else None,
        "market": market,
        "days": days,
        "items": rows,
    }


# ── PR-4b: health metrics + auto-promote settings ────────────────


@router.get("/strategies/{template_id}/health")
async def get_strategy_health(
    template_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = 30,
):
    """PR-4b: rolling health metric history for the UI sparkline.
    Returns newest-first; the cron writes one row per UTC day, so
    `days=30` is ~30 rows. Owner-scoped."""
    from services import strategy_health_service as hsvc
    from services import strategy_template_service as tsvc
    row = await tsvc.get_template(
        db, template_id=template_id,
        owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    snapshots = await hsvc.list_recent_snapshots(
        db, strategy_id=template_id, days=max(int(days), 1),
    )
    return {
        "strategy_id": str(template_id),
        "days": days,
        "snapshots": [hsvc.snapshot_to_dict(s) for s in snapshots],
    }


@router.patch("/strategies/{template_id}/auto-promote")
async def patch_auto_promote_settings(
    template_id: uuid.UUID,
    body: dict,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PR-4b: opt-in / tighten the walk-forward auto-promote
    thresholds for a strategy. Owner-scoped; validates the input
    ranges so a bad value doesn't poison the gate."""
    from services import strategy_template_service as tsvc
    row = await tsvc.get_template(
        db, template_id=template_id,
        owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")

    if "enabled" in body:
        row.auto_promote_enabled = bool(body["enabled"])
    if "min_brier_improvement" in body:
        try:
            v = float(body["min_brier_improvement"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="min_brier_improvement must be a number",
            )
        if v < 0.0 or v > 1.0:
            raise HTTPException(
                status_code=400,
                detail="min_brier_improvement must be in [0, 1]",
            )
        row.auto_promote_min_oos_brier_improvement = v
    if "min_hit_rate" in body:
        try:
            v = float(body["min_hit_rate"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="min_hit_rate must be a number",
            )
        if v < 0.0 or v > 1.0:
            raise HTTPException(
                status_code=400,
                detail="min_hit_rate must be in [0, 1]",
            )
        row.auto_promote_min_oos_hit_rate = v
    await db.commit()
    await db.refresh(row)
    return {
        "strategy_id": str(template_id),
        "auto_promote_enabled": row.auto_promote_enabled,
        "min_brier_improvement": row.auto_promote_min_oos_brier_improvement,
        "min_hit_rate": row.auto_promote_min_oos_hit_rate,
    }


@router.get(
    "/strategies/{template_id}/aggregate",
    response_model=SweepAggregateResponse,
)
async def aggregate_strategy_route(
    template_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Cross-sweep KPIs for every sweep that referenced this
    template. Soft-deleted templates are still aggregatable so a
    purged strategy's history stays inspectable."""
    from services import strategy_template_service as tsvc
    from services import sweep_aggregate_service as agg

    row = await tsvc.get_template(
        db, template_id=template_id,
        owner_id=_coerce_owner_uuid(user),
        include_deleted=True,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    payload = await agg.aggregate_strategy(
        db, owner_id=_coerce_owner_uuid(user), strategy_id=template_id,
    )
    return SweepAggregateResponse(**payload)


@router.post(
    "/strategies/{template_id}/learn",
    response_model=PersonaWeightLearnResponse,
)
async def learn_strategy_weights(
    template_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PR-C: recompute persona weights from this template's
    aggregate history. Sweep completion auto-triggers this for
    its parent template; this manual endpoint exists so the
    operator can force a re-learn after editing the roster or
    importing external sweeps."""
    from services import persona_weight_learner
    try:
        result = await persona_weight_learner.learn_weights_for_strategy(
            db,
            owner_id=_coerce_owner_uuid(user),
            strategy_id=template_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return PersonaWeightLearnResponse(**result)


@router.get(
    "/strategies/{template_id}/brier-history",
    response_model=list[StrategyBrierHistoryPoint],
)
async def strategy_brier_history(
    template_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    window_days: int = 90,
):
    """Per-sweep Brier time-series for one strategy template,
    used by the frontend's BrierTrendChart to show whether the
    learning loop is actually reducing prediction error over
    time. Default window 90 days; clamp to [7, 730] so a
    rogue caller can't trigger a full-table scan.

    Owner-scoped + soft-delete-tolerant (matches /aggregate's
    permissions). Returns `[]` when no completed sweep with a
    resolved discussion exists in the window — frontend renders
    a "data still warming up" placeholder."""
    if window_days < 7 or window_days > 730:
        raise HTTPException(
            status_code=400,
            detail=f"window_days must be in [7, 730], got {window_days}",
        )
    from services import strategy_template_service as tsvc
    from services import sweep_aggregate_service as agg

    row = await tsvc.get_template(
        db, template_id=template_id,
        owner_id=_coerce_owner_uuid(user),
        include_deleted=True,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    points = await agg.fetch_strategy_brier_history(
        db,
        owner_id=_coerce_owner_uuid(user),
        strategy_id=template_id,
        window_days=window_days,
    )
    return [StrategyBrierHistoryPoint(**p) for p in points]


@router.delete(
    "/strategies/{template_id}",
    status_code=204,
)
async def delete_strategy(
    template_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Soft-delete: future sweeps that reference this template can
    still resolve `template.name` for display in the dashboard."""
    from services import strategy_template_service as tsvc
    row = await tsvc.get_template(
        db, template_id=template_id,
        owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    await tsvc.soft_delete_template(db, row)
    return None


@router.post(
    "/strategies/{template_id}/walk-forward",
    response_model=WalkForwardPlanResponse,
)
async def kick_off_walk_forward(
    template_id: uuid.UUID,
    body: WalkForwardRequest,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PR-A1 follow-up — operator-triggered walk-forward run.

    Resolves the rolling (train, test) fold plan synchronously
    (so 400 surfaces immediately if the archive can't reach the
    requested span) and detaches the orchestrator into the event
    loop. The HTTP request returns the plan + `started=True`; the
    actual sweep rows appear via `GET /sweeps?strategy_id=...` as
    the worker creates them.

    Failure modes:
      - 404 — strategy template doesn't exist or doesn't belong
        to the caller
      - 400 — anchor_date can't be parsed, or
        `plan_walk_forward` rejects the requested span (archive
        too thin, invalid combination)
    """
    from datetime import date as _date

    from services import strategy_template_service as tsvc
    from services import walk_forward_service as wf

    owner_uuid = _coerce_owner_uuid(user)
    tmpl = await tsvc.get_template(
        db, template_id=template_id, owner_id=owner_uuid,
    )
    if tmpl is None:
        raise HTTPException(
            status_code=404, detail="Strategy not found",
        )

    # Audit follow-up #2: refuse to spawn a second orchestrator
    # while the first is still in flight. Two parallel runs would
    # each create train+test sweeps for the same strategy, race
    # on weight learning, and burn LLM quota proportionally.
    # Operator hits this when they double-click the trigger
    # button or a stale browser tab re-fires the request.
    if await wf.has_active_walk_forward(
        db, strategy_id=template_id,
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "A walk-forward run is already in flight for this "
                "strategy. Wait for it to complete or cancel its "
                "fold sweeps via /sweeps/{id}/cancel before starting "
                "another."
            ),
        )

    try:
        anchor = _date.fromisoformat(body.anchor_date)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"anchor_date must be ISO YYYY-MM-DD: {exc}",
        )

    try:
        plan = await wf.plan_walk_forward(
            db,
            strategy_id=template_id,
            market=tmpl.market,
            anchor_date=anchor,
            train_window_days=body.train_window_days,
            test_window_days=body.test_window_days,
            n_folds=body.n_folds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    wf.execute_walk_forward_in_background(
        owner_id=owner_uuid,
        strategy_id=template_id,
        plan=plan,
        rounds_per_discussion=body.rounds_per_discussion,
        concurrency=body.concurrency,
        auto_post_mortem=body.auto_post_mortem,
    )

    return WalkForwardPlanResponse(
        strategy_id=plan.strategy_id,
        market=plan.market,
        train_window_days=plan.train_window_days,
        test_window_days=plan.test_window_days,
        folds=[
            WalkForwardFoldOut(
                fold_index=f.fold_index,
                train_anchor=f.train_anchor.isoformat(),
                train_dates=[d.isoformat() for d in f.train_dates],
                test_anchor=f.test_anchor.isoformat(),
                test_dates=[d.isoformat() for d in f.test_dates],
            )
            for f in plan.folds
        ],
        started=True,
    )
