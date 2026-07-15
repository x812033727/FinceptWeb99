"""Persistence and promotion governance for TW multi-factor research."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.tw_factor_research import TwFactorModelVersion, TwFactorResearchRun
from models.user import User
from services.tw_factor_service import FACTOR_NAMES, PROFILES

PROMOTION_THRESHOLDS: dict[str, float | int] = {
    "minimum_periods": 24,
    "minimum_adaptive_periods": 12,
    "minimum_composite_rank_ic": 0.03,
    "minimum_average_excess_return_pct": 0.0,
    "minimum_positive_excess_rate_pct": 55.0,
    "minimum_max_drawdown_pct": -20.0,
    "minimum_average_fill_pct": 80.0,
    "maximum_weight_turnover_pct": 10.0,
    "minimum_champion_excess_improvement_pct": 0.10,
    "maximum_champion_ic_regression": 0.01,
}
BLOCKING_QUALITY_FLAGS = {
    "survivorship_bias",
    "unadjusted_price_history",
    "partial_adjusted_price_history",
    "sector_classification_not_point_in_time",
    "sector_neutralization_unavailable",
    "price_limit_history_unavailable",
    "suspension_history_unavailable",
    "low_factor_forward_return_coverage",
}


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def extract_model_metrics(result: dict[str, Any]) -> dict[str, Any]:
    summary = result.get("summary") or {}
    diagnostic = (result.get("factor_diagnostics") or {}).get("composite") or {}
    stability = result.get("weight_stability") or {}
    quality = result.get("quality") or {}
    return {
        "period_count": int(summary.get("period_count") or 0),
        "adaptive_period_count": int(stability.get("adaptive_period_count") or 0),
        "average_excess_return_pct": _number(summary.get("average_excess_return_pct")),
        "positive_excess_rate_pct": _number(summary.get("positive_excess_rate_pct")),
        "max_drawdown_pct": _number(summary.get("max_drawdown_pct")),
        "average_fill_pct": _number(summary.get("average_fill_pct")),
        "composite_rank_ic": _number(diagnostic.get("average_rank_ic")),
        "composite_holm_significant": bool(
            diagnostic.get("significant_after_holm_5pct", False)
        ),
        "maximum_weight_turnover_pct": _number(
            stability.get("maximum_weight_turnover_pct")
        ),
        "benchmark_used": result.get("benchmark_used"),
        "benchmark_requested": result.get("benchmark_requested"),
        "quality_status": quality.get("status"),
        "quality_flags": list(quality.get("flags") or []),
    }


def extract_candidate_weights(result: dict[str, Any]) -> dict[str, float]:
    profile = str(result.get("profile") or "balanced")
    base = PROFILES.get(profile, PROFILES["balanced"])
    # The final reported period can be a data-quality fallback. Prefer the
    # most recent genuinely adaptive fold so the registered challenger is
    # the learned model that passed the gate, not an incidental base profile.
    latest_adaptive = next((
        period for period in reversed(result.get("periods") or [])
        if period.get("weight_fallback_reason") is None
        and period.get("factor_weights")
    ), None)
    if latest_adaptive:
        weights = {
            factor: _number((latest_adaptive.get("factor_weights") or {}).get(factor))
            for factor in FACTOR_NAMES
        }
    else:
        ranges = (result.get("weight_stability") or {}).get("factor_ranges") or {}
        weights = {
            factor: _number((ranges.get(factor) or {}).get("latest"))
            for factor in FACTOR_NAMES
        }
    if any(value is None or value < 0 for value in weights.values()):
        return dict(base)
    total = sum(float(value) for value in weights.values() if value is not None)
    if total <= 0:
        return dict(base)
    return {factor: round(float(value) / total, 8) for factor, value in weights.items()}


def evaluate_promotion_gate(
    metrics: dict[str, Any], champion_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return auditable absolute and champion-relative promotion checks."""
    checks: list[dict[str, Any]] = []

    def minimum(name: str, value: Any, threshold: float | int) -> None:
        actual = _number(value)
        checks.append({
            "name": name, "actual": actual, "operator": ">=", "threshold": threshold,
            "passed": actual is not None and actual >= threshold,
        })

    def maximum(name: str, value: Any, threshold: float | int) -> None:
        actual = _number(value)
        checks.append({
            "name": name, "actual": actual, "operator": "<=", "threshold": threshold,
            "passed": actual is not None and actual <= threshold,
        })

    minimum("period_count", metrics.get("period_count"), PROMOTION_THRESHOLDS["minimum_periods"])
    minimum(
        "adaptive_period_count", metrics.get("adaptive_period_count"),
        PROMOTION_THRESHOLDS["minimum_adaptive_periods"],
    )
    minimum(
        "composite_rank_ic", metrics.get("composite_rank_ic"),
        PROMOTION_THRESHOLDS["minimum_composite_rank_ic"],
    )
    minimum(
        "average_excess_return_pct", metrics.get("average_excess_return_pct"),
        PROMOTION_THRESHOLDS["minimum_average_excess_return_pct"],
    )
    minimum(
        "positive_excess_rate_pct", metrics.get("positive_excess_rate_pct"),
        PROMOTION_THRESHOLDS["minimum_positive_excess_rate_pct"],
    )
    minimum(
        "max_drawdown_pct", metrics.get("max_drawdown_pct"),
        PROMOTION_THRESHOLDS["minimum_max_drawdown_pct"],
    )
    minimum(
        "average_fill_pct", metrics.get("average_fill_pct"),
        PROMOTION_THRESHOLDS["minimum_average_fill_pct"],
    )
    maximum(
        "maximum_weight_turnover_pct", metrics.get("maximum_weight_turnover_pct"),
        PROMOTION_THRESHOLDS["maximum_weight_turnover_pct"],
    )
    benchmark_passed = (
        metrics.get("benchmark_used") == "taiex_total_return"
        and metrics.get("benchmark_requested") == "taiex_total_return"
    )
    checks.append({
        "name": "independent_total_return_benchmark",
        "actual": metrics.get("benchmark_used"), "operator": "==",
        "threshold": "taiex_total_return", "passed": benchmark_passed,
    })
    checks.append({
        "name": "holm_significant_composite_ic",
        "actual": bool(metrics.get("composite_holm_significant")), "operator": "==",
        "threshold": True, "passed": bool(metrics.get("composite_holm_significant")),
    })
    blocking_flags = sorted(
        set(metrics.get("quality_flags") or []) & BLOCKING_QUALITY_FLAGS
    )
    checks.append({
        "name": "material_data_quality_flags",
        "actual": blocking_flags, "operator": "==", "threshold": [],
        "passed": not blocking_flags,
    })

    if champion_metrics:
        champion_excess = _number(champion_metrics.get("average_excess_return_pct"))
        candidate_excess = _number(metrics.get("average_excess_return_pct"))
        required_excess = (
            champion_excess
            + float(PROMOTION_THRESHOLDS["minimum_champion_excess_improvement_pct"])
            if champion_excess is not None else None
        )
        checks.append({
            "name": "champion_excess_improvement",
            "actual": candidate_excess, "operator": ">=", "threshold": required_excess,
            "passed": (
                candidate_excess is not None and required_excess is not None
                and candidate_excess >= required_excess
            ),
        })
        champion_ic = _number(champion_metrics.get("composite_rank_ic"))
        candidate_ic = _number(metrics.get("composite_rank_ic"))
        minimum_ic = (
            champion_ic - float(PROMOTION_THRESHOLDS["maximum_champion_ic_regression"])
            if champion_ic is not None else None
        )
        checks.append({
            "name": "champion_ic_non_regression",
            "actual": candidate_ic, "operator": ">=", "threshold": minimum_ic,
            "passed": (
                candidate_ic is not None and minimum_ic is not None
                and candidate_ic >= minimum_ic
            ),
        })

    failed = [check["name"] for check in checks if not check["passed"]]
    return {
        "eligible": not failed,
        "checks": checks,
        "failed_checks": failed,
        "threshold_version": "tw-factor-promotion-v1",
    }


async def get_champion(
    db: AsyncSession, user_id: UUID, profile: str,
) -> TwFactorModelVersion | None:
    return await db.scalar(
        select(TwFactorModelVersion).where(
            TwFactorModelVersion.user_id == user_id,
            TwFactorModelVersion.profile == profile,
            TwFactorModelVersion.status == "champion",
        ).order_by(TwFactorModelVersion.version_number.desc()).limit(1)
    )


async def save_research_result(
    db: AsyncSession, *, user_id: UUID, name: str | None,
    parameters: dict[str, Any], result: dict[str, Any], auto_promote: bool,
) -> tuple[TwFactorResearchRun, TwFactorModelVersion]:
    """Atomically save a run, create its challenger, and optionally promote it."""
    # Serialize version allocation and champion replacement per user.
    await db.scalar(select(User).where(User.id == user_id).with_for_update())
    profile = str(result["profile"])
    champion = await get_champion(db, user_id, profile)
    metrics = extract_model_metrics(result)
    gate = evaluate_promotion_gate(metrics, champion.metrics if champion else None)
    run = TwFactorResearchRun(
        user_id=user_id, name=(name or None), profile=profile,
        methodology_version=str(result["methodology_version"]),
        parameters=parameters, summary=result.get("summary") or {},
        gate_result=gate, result=result,
    )
    db.add(run)
    await db.flush()
    latest_version = await db.scalar(
        select(func.max(TwFactorModelVersion.version_number)).where(
            TwFactorModelVersion.user_id == user_id,
            TwFactorModelVersion.profile == profile,
        )
    )
    model = TwFactorModelVersion(
        user_id=user_id, profile=profile,
        version_number=int(latest_version or 0) + 1,
        methodology_version=str(result["methodology_version"]),
        status="candidate", weights=extract_candidate_weights(result), metrics=metrics,
        gate_result=gate, source_run_id=run.id,
    )
    db.add(model)
    await db.flush()
    if auto_promote and gate["eligible"]:
        await _promote(db, model, note="auto-promoted after all governance gates passed")
    await db.commit()
    await db.refresh(run)
    await db.refresh(model)
    return run, model


async def _promote(
    db: AsyncSession, model: TwFactorModelVersion, *, note: str,
) -> None:
    await db.execute(
        update(TwFactorModelVersion).where(
            TwFactorModelVersion.user_id == model.user_id,
            TwFactorModelVersion.profile == model.profile,
            TwFactorModelVersion.status == "champion",
            TwFactorModelVersion.id != model.id,
        ).values(status="retired")
    )
    model.status = "champion"
    model.promoted_at = datetime.now(UTC)
    model.promotion_note = note


async def promote_model(
    db: AsyncSession, *, user_id: UUID, model_id: UUID,
) -> TwFactorModelVersion | None:
    await db.scalar(select(User).where(User.id == user_id).with_for_update())
    model = await db.get(TwFactorModelVersion, model_id)
    if model is None or model.user_id != user_id:
        return None
    if model.status == "champion":
        return model
    champion = await get_champion(db, user_id, model.profile)
    gate = evaluate_promotion_gate(
        model.metrics or {}, champion.metrics if champion else None,
    )
    model.gate_result = gate
    if not gate["eligible"]:
        await db.commit()
        raise ValueError("model has not passed every promotion gate")
    await _promote(db, model, note="manually promoted after governance gates passed")
    await db.commit()
    await db.refresh(model)
    return model


async def list_runs(
    db: AsyncSession, user_id: UUID, *, limit: int = 20, offset: int = 0,
) -> tuple[list[TwFactorResearchRun], int]:
    predicate = TwFactorResearchRun.user_id == user_id
    total = await db.scalar(select(func.count()).select_from(TwFactorResearchRun).where(predicate))
    rows = (await db.scalars(
        select(TwFactorResearchRun).where(predicate)
        .order_by(TwFactorResearchRun.created_at.desc(), TwFactorResearchRun.id.desc())
        .limit(limit).offset(offset)
    )).all()
    return list(rows), int(total or 0)


async def get_run(
    db: AsyncSession, user_id: UUID, run_id: UUID,
) -> TwFactorResearchRun | None:
    row = await db.get(TwFactorResearchRun, run_id)
    return row if row is not None and row.user_id == user_id else None


async def list_models(
    db: AsyncSession, user_id: UUID, *, profile: str | None = None,
) -> list[TwFactorModelVersion]:
    query = select(TwFactorModelVersion).where(TwFactorModelVersion.user_id == user_id)
    if profile:
        query = query.where(TwFactorModelVersion.profile == profile)
    rows = (await db.scalars(query.order_by(
        TwFactorModelVersion.profile.asc(), TwFactorModelVersion.version_number.desc(),
    ))).all()
    return list(rows)
