"""PR-4b: tests for `evaluate_walk_forward_for_promotion` —
the gate that decides whether walk-forward test fold weights
get auto-deployed to the strategy's live `persona_weights`.
"""
from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.user import User, UserRole
from services import walk_forward_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"wfap-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_strategy(
    db: AsyncSession, owner_id: uuid.UUID,
    *,
    auto_promote_enabled: bool = True,
    min_brier_improvement: float = 0.0,
    min_hit_rate: float = 0.5,
    initial_weights: dict | None = None,
) -> DiscussionStrategyTemplate:
    tmpl = DiscussionStrategyTemplate(
        id=uuid4(), owner_id=owner_id,
        name="t", topic="x", rules="", market="TW",
        persona_ids=["a", "b"],
        persona_weights=initial_weights,
        auto_promote_enabled=auto_promote_enabled,
        auto_promote_min_oos_brier_improvement=min_brier_improvement,
        auto_promote_min_oos_hit_rate=min_hit_rate,
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


async def _make_sweep(
    db: AsyncSession, owner_id: uuid.UUID, strategy_id: uuid.UUID,
    *, fold_kind: str = "test",
) -> BacktestSweep:
    sweep = BacktestSweep(
        id=uuid4(), owner_id=owner_id, strategy_id=strategy_id,
        market="TW", topic="x", rules="",
        persona_ids=["a", "b"],
        anchor_date=date(2026, 4, 1),
        trading_days_count=5, rounds_per_discussion=1,
        concurrency=1, status="completed",
        fold_kind=fold_kind,
    )
    db.add(sweep)
    await db.commit()
    await db.refresh(sweep)
    return sweep


# ── early-return guards ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_when_auto_promote_disabled(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(
        db_session, owner.id, auto_promote_enabled=False,
    )
    train = await _make_sweep(db_session, owner.id, tmpl.id, fold_kind="train")
    test = await _make_sweep(db_session, owner.id, tmpl.id, fold_kind="test")

    with patch.object(svc, "_verify_test_fold_discussions", AsyncMock(return_value=0)):
        result = await svc.evaluate_walk_forward_for_promotion(
            owner_id=owner.id,
            strategy_id=tmpl.id,
            train_sweep_id=train.id,
            test_sweep_id=test.id,
            weights={"a": 1.5, "b": 0.8},
        )
    assert result["promoted"] is False
    assert result["reason"] == "auto_promote_disabled"
    await db_session.refresh(tmpl)
    assert tmpl.persona_weights is None


@pytest.mark.asyncio
async def test_skips_when_weights_empty(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    train = await _make_sweep(db_session, owner.id, tmpl.id, fold_kind="train")
    test = await _make_sweep(db_session, owner.id, tmpl.id, fold_kind="test")
    with patch.object(svc, "_verify_test_fold_discussions", AsyncMock(return_value=0)):
        result = await svc.evaluate_walk_forward_for_promotion(
            owner_id=owner.id,
            strategy_id=tmpl.id,
            train_sweep_id=train.id,
            test_sweep_id=test.id,
            weights={},
        )
    assert result["promoted"] is False
    assert result["reason"] == "empty_weights"


# ── KPI gate logic ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_when_test_kpis_unresolved(
    db_session: AsyncSession, owner: User,
):
    """Aggregate returns None brier / win_rate (test fold has no
    graded discussions yet) → can't decide → don't promote."""
    tmpl = await _make_strategy(db_session, owner.id)
    train = await _make_sweep(db_session, owner.id, tmpl.id, fold_kind="train")
    test = await _make_sweep(db_session, owner.id, tmpl.id, fold_kind="test")

    with patch.object(svc, "_verify_test_fold_discussions", AsyncMock(return_value=0)), \
         patch(
             "services.sweep_aggregate_service.aggregate_sweep",
             AsyncMock(return_value={"brier_score": None, "win_rate": None}),
         ):
        result = await svc.evaluate_walk_forward_for_promotion(
            owner_id=owner.id,
            strategy_id=tmpl.id,
            train_sweep_id=train.id,
            test_sweep_id=test.id,
            weights={"a": 1.5},
        )
    assert result["promoted"] is False
    assert result["reason"] == "test_fold_kpi_unresolved"


@pytest.mark.asyncio
async def test_skips_when_hit_rate_below_threshold(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(
        db_session, owner.id,
        min_hit_rate=0.6,
    )
    train = await _make_sweep(db_session, owner.id, tmpl.id, fold_kind="train")
    test = await _make_sweep(db_session, owner.id, tmpl.id, fold_kind="test")
    with patch.object(svc, "_verify_test_fold_discussions", AsyncMock(return_value=0)), \
         patch(
             "services.sweep_aggregate_service.aggregate_sweep",
             AsyncMock(return_value={"brier_score": 0.18, "win_rate": 0.55}),
         ):
        result = await svc.evaluate_walk_forward_for_promotion(
            owner_id=owner.id,
            strategy_id=tmpl.id,
            train_sweep_id=train.id,
            test_sweep_id=test.id,
            weights={"a": 1.5},
        )
    assert result["promoted"] is False
    assert "test_win_rate" in result["reason"]
    assert "0.550" in result["reason"]


@pytest.mark.asyncio
async def test_skips_when_brier_improvement_insufficient(
    db_session: AsyncSession, owner: User,
):
    """Baseline 0.20, test 0.19 → improvement 0.01 < threshold 0.05."""
    from models.strategy_health_metric import StrategyHealthMetric
    tmpl = await _make_strategy(
        db_session, owner.id,
        min_brier_improvement=0.05,
    )
    db_session.add(StrategyHealthMetric(
        strategy_id=tmpl.id,
        snapshot_date=date.today(),
        brier_30d=0.20,
        sample_count_30d=10,
        status_flags=[],
    ))
    await db_session.commit()

    train = await _make_sweep(db_session, owner.id, tmpl.id, fold_kind="train")
    test = await _make_sweep(db_session, owner.id, tmpl.id, fold_kind="test")
    with patch.object(svc, "_verify_test_fold_discussions", AsyncMock(return_value=0)), \
         patch(
             "services.sweep_aggregate_service.aggregate_sweep",
             AsyncMock(return_value={"brier_score": 0.19, "win_rate": 0.7}),
         ):
        result = await svc.evaluate_walk_forward_for_promotion(
            owner_id=owner.id,
            strategy_id=tmpl.id,
            train_sweep_id=train.id,
            test_sweep_id=test.id,
            weights={"a": 1.5},
        )
    assert result["promoted"] is False
    assert "brier improvement" in result["reason"]


@pytest.mark.asyncio
async def test_promotes_when_all_gates_pass(
    db_session: AsyncSession, owner: User,
):
    """Test fold passes both gates → weights auto-deployed +
    version row recorded with provenance notes."""
    from models.strategy_health_metric import StrategyHealthMetric
    from models.strategy_version import StrategyVersion
    from sqlalchemy import select
    tmpl = await _make_strategy(
        db_session, owner.id,
        min_brier_improvement=0.0,
        min_hit_rate=0.5,
    )
    db_session.add(StrategyHealthMetric(
        strategy_id=tmpl.id,
        snapshot_date=date.today(),
        brier_30d=0.25,
        sample_count_30d=10,
        status_flags=[],
    ))
    await db_session.commit()

    train = await _make_sweep(db_session, owner.id, tmpl.id, fold_kind="train")
    test = await _make_sweep(db_session, owner.id, tmpl.id, fold_kind="test")
    new_weights = {"a": 1.6, "b": 0.9}

    with patch.object(svc, "_verify_test_fold_discussions", AsyncMock(return_value=0)), \
         patch(
             "services.sweep_aggregate_service.aggregate_sweep",
             AsyncMock(return_value={"brier_score": 0.18, "win_rate": 0.65}),
         ):
        result = await svc.evaluate_walk_forward_for_promotion(
            owner_id=owner.id,
            strategy_id=tmpl.id,
            train_sweep_id=train.id,
            test_sweep_id=test.id,
            weights=new_weights,
        )
    assert result["promoted"] is True
    assert result["reason"] == "kpis_passed"
    # Live column updated
    await db_session.refresh(tmpl)
    assert tmpl.persona_weights == new_weights
    # Version row recorded with notes describing OOS provenance
    versions = list((await db_session.scalars(
        select(StrategyVersion).where(
            StrategyVersion.strategy_id == tmpl.id,
            StrategyVersion.artifact_kind == "weights",
        ),
    )).all())
    assert len(versions) == 1
    assert "auto-promoted" in (versions[0].notes or "")
    assert versions[0].source_sweep_id == test.id


@pytest.mark.asyncio
async def test_promotes_with_no_baseline_when_threshold_zero(
    db_session: AsyncSession, owner: User,
):
    """Cold strategy with no health snapshot yet — baseline_brier
    is None. With threshold=0 the gate should still pass (we
    interpret this as 'any non-degraded' instead of locking out
    the very first promotion)."""
    tmpl = await _make_strategy(
        db_session, owner.id,
        min_brier_improvement=0.0,
        min_hit_rate=0.5,
    )
    train = await _make_sweep(db_session, owner.id, tmpl.id, fold_kind="train")
    test = await _make_sweep(db_session, owner.id, tmpl.id, fold_kind="test")
    with patch.object(svc, "_verify_test_fold_discussions", AsyncMock(return_value=0)), \
         patch(
             "services.sweep_aggregate_service.aggregate_sweep",
             AsyncMock(return_value={"brier_score": 0.20, "win_rate": 0.6}),
         ):
        result = await svc.evaluate_walk_forward_for_promotion(
            owner_id=owner.id,
            strategy_id=tmpl.id,
            train_sweep_id=train.id,
            test_sweep_id=test.id,
            weights={"a": 1.5},
        )
    assert result["promoted"] is True
    assert result["baseline_brier"] is None


@pytest.mark.asyncio
async def test_unknown_strategy_returns_skip_not_raise(
    db_session: AsyncSession, owner: User,
):
    bogus = uuid4()
    sweep = await _make_sweep(db_session, owner.id, uuid4(), fold_kind="train")
    with patch.object(svc, "_verify_test_fold_discussions", AsyncMock(return_value=0)):
        result = await svc.evaluate_walk_forward_for_promotion(
            owner_id=owner.id,
            strategy_id=bogus,
            train_sweep_id=sweep.id,
            test_sweep_id=sweep.id,
            weights={"a": 1.5},
        )
    assert result["promoted"] is False
    assert result["reason"] == "strategy_not_found"
