from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User, UserRole
from services import tw_factor_registry_service as svc


def _result(*, excess: float = .5, ic: float = .08, eligible: bool = True) -> dict:
    return {
        "market": "TW", "profile": "balanced",
        "methodology_version": "tw-explainable-multifactor-v8",
        "benchmark_requested": "taiex_total_return",
        "benchmark_used": "taiex_total_return",
        "summary": {
            "period_count": 30 if eligible else 10,
            "average_excess_return_pct": excess,
            "positive_excess_rate_pct": 60,
            "max_drawdown_pct": -8,
            "average_fill_pct": 92,
        },
        "factor_diagnostics": {
            "composite": {
                "average_rank_ic": ic,
                "significant_after_holm_5pct": True,
            },
        },
        "weight_stability": {
            "adaptive_period_count": 18 if eligible else 0,
            "maximum_weight_turnover_pct": 4,
            "factor_ranges": {
                "value": {"latest": .24}, "quality": {"latest": .17},
                "momentum": {"latest": .19}, "low_volatility": {"latest": .15},
                "income": {"latest": .10}, "liquidity": {"latest": .15},
            },
        },
        "quality": {"status": "good", "flags": []},
        # Detail payload fields are not interpreted by registry persistence.
        "periods": [], "start_date": "2021-01-01", "end_date": "2025-01-01",
    }


def test_promotion_gate_requires_absolute_and_champion_relative_improvement():
    candidate = svc.extract_model_metrics(_result(excess=.5, ic=.08))
    first = svc.evaluate_promotion_gate(candidate)
    assert first["eligible"] is True

    champion = svc.extract_model_metrics(_result(excess=.45, ic=.08))
    relative = svc.evaluate_promotion_gate(candidate, champion)
    assert relative["eligible"] is False
    assert "champion_excess_improvement" in relative["failed_checks"]

    improved = svc.extract_model_metrics(_result(excess=.6, ic=.075))
    assert svc.evaluate_promotion_gate(improved, champion)["eligible"] is True


def test_candidate_weights_are_normalized_and_fall_back_safely():
    weights = svc.extract_candidate_weights(_result())
    assert sum(weights.values()) == pytest.approx(1)
    assert weights["quality"] == pytest.approx(.17)
    malformed = _result()
    malformed["weight_stability"]["factor_ranges"]["quality"]["latest"] = None
    assert svc.extract_candidate_weights(malformed) == svc.PROFILES["balanced"]

    with_final_fallback = _result()
    with_final_fallback["periods"] = [
        {
            "weight_fallback_reason": None,
            "factor_weights": {
                "value": .20, "quality": .20, "momentum": .20,
                "low_volatility": .15, "income": .10, "liquidity": .15,
            },
        },
        {
            "weight_fallback_reason": "insufficient_factor_coverage",
            "factor_weights": svc.PROFILES["balanced"],
        },
    ]
    assert svc.extract_candidate_weights(with_final_fallback)["quality"] == pytest.approx(.20)


def test_promotion_gate_rejects_material_point_in_time_bias():
    result = _result()
    result["quality"]["flags"] = ["survivorship_bias"]
    gate = svc.evaluate_promotion_gate(svc.extract_model_metrics(result))
    assert gate["eligible"] is False
    assert "material_data_quality_flags" in gate["failed_checks"]


@pytest.mark.asyncio
async def test_registry_versions_promotes_and_retires_champion(db_session: AsyncSession):
    user = User(
        id=uuid4(), email="factor-registry@example.com", hashed_password="x",
        role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()

    first_run, first_model = await svc.save_research_result(
        db_session, user_id=user.id, name="first", parameters={"top_n": 20},
        result=_result(excess=.45), auto_promote=True,
    )
    assert first_model.status == "champion"
    assert first_model.version_number == 1
    assert first_run.gate_result["eligible"] is True

    _, second_model = await svc.save_research_result(
        db_session, user_id=user.id, name="better", parameters={"top_n": 20},
        result=_result(excess=.60), auto_promote=True,
    )
    await db_session.refresh(first_model)
    assert first_model.status == "retired"
    assert second_model.status == "champion"
    assert second_model.version_number == 2
    assert (await svc.get_champion(db_session, user.id, "balanced")).id == second_model.id


@pytest.mark.asyncio
async def test_registry_does_not_promote_failed_or_cross_tenant_model(
    db_session: AsyncSession,
):
    owner = User(email="factor-owner@example.com", hashed_password="x")
    outsider = User(email="factor-outsider@example.com", hashed_password="x")
    db_session.add_all([owner, outsider])
    await db_session.commit()
    run, model = await svc.save_research_result(
        db_session, user_id=owner.id, name=None, parameters={},
        result=_result(eligible=False), auto_promote=True,
    )
    assert model.status == "candidate"
    assert "period_count" in model.gate_result["failed_checks"]
    with pytest.raises(ValueError, match="promotion gate"):
        await svc.promote_model(db_session, user_id=owner.id, model_id=model.id)
    assert await svc.promote_model(
        db_session, user_id=outsider.id, model_id=model.id,
    ) is None
    assert await svc.get_run(db_session, outsider.id, run.id) is None


@pytest.mark.asyncio
async def test_manual_promotion_rechecks_against_current_champion(db_session: AsyncSession):
    user = User(email="factor-stale-gate@example.com", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    _, stale = await svc.save_research_result(
        db_session, user_id=user.id, name="stale", parameters={},
        result=_result(excess=.5), auto_promote=False,
    )
    assert stale.gate_result["eligible"] is True
    _, champion = await svc.save_research_result(
        db_session, user_id=user.id, name="champion", parameters={},
        result=_result(excess=.6), auto_promote=True,
    )
    assert champion.status == "champion"
    with pytest.raises(ValueError, match="promotion gate"):
        await svc.promote_model(db_session, user_id=user.id, model_id=stale.id)
    await db_session.refresh(stale)
    assert "champion_excess_improvement" in stale.gate_result["failed_checks"]
