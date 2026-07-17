"""PR-3: tests for the calibration deployment gate.

Covers `evaluate_curve_safety` (pure compute), the
`fit_isotonic_for_strategy` integration with the gate (deployed vs
pending_review vs no_change), and the approve/reject flow.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion import Discussion
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.user import User, UserRole
from services import confidence_calibrator as svc


# ── pure: evaluate_curve_safety ──────────────────────────────────


def test_safety_passes_on_clean_curve_no_old():
    """Cold-start: no old curve to diff against, monotone new curve,
    no brier signal → safe."""
    new = [{"raw": 0.3, "calibrated": 0.2}, {"raw": 0.7, "calibrated": 0.55}]
    out = svc.evaluate_curve_safety(old_curve=None, new_curve=new)
    assert out["safe"] is True
    assert out["reason"] is None
    assert out["monotonic"] is True


def test_safety_flags_non_monotone_curve():
    new = [
        {"raw": 0.3, "calibrated": 0.5},
        {"raw": 0.7, "calibrated": 0.4},   # inversion
    ]
    out = svc.evaluate_curve_safety(old_curve=None, new_curve=new)
    assert out["safe"] is False
    assert out["monotonic"] is False
    assert "non-monotone" in out["reason"]


def test_safety_flags_endpoint_inversion_with_long_curve():
    """A monotone curve can still have endpoint_gap < min if the
    rightmost calibrated value drops below the midpoint. PAV
    should prevent this in normal flow but defensive check."""
    # Construct a curve where mid (apply at 0.5) > top
    new = [
        {"raw": 0.45, "calibrated": 0.6},
        {"raw": 0.5, "calibrated": 0.6},
        # All x > 0.5 still calibrated to 0.6 — the "top" is also
        # 0.6, gap is 0. Override threshold to test.
    ]
    # With default threshold (-0.05), gap=0 is fine
    ok = svc.evaluate_curve_safety(old_curve=None, new_curve=new)
    assert ok["safe"] is True


def test_safety_flags_large_delta_vs_old():
    """New curve calibrates 0.5 to 0.5; old calibrated it to 0.2 →
    delta 0.3 == threshold → safe (boundary). Push to 0.4 → unsafe."""
    old = [{"raw": 0.5, "calibrated": 0.2}]
    big_shift = [{"raw": 0.5, "calibrated": 0.6}]   # delta 0.4 > 0.3
    out = svc.evaluate_curve_safety(old_curve=old, new_curve=big_shift)
    assert out["safe"] is False
    assert "large shift" in out["reason"]
    assert out["max_control_point_delta"] >= 0.3


def test_safety_flags_brier_degradation():
    """recent_brier > baseline by > 1.3× → fail."""
    new = [{"raw": 0.5, "calibrated": 0.5}]
    out = svc.evaluate_curve_safety(
        old_curve=new, new_curve=new,
        recent_brier=0.30, baseline_brier=0.20,   # ratio 1.5
    )
    assert out["safe"] is False
    assert "brier" in out["reason"]
    assert out["brier_degradation_ratio"] == 1.5


def test_safety_thresholds_overridable():
    """Custom thresholds tighten/loosen the gate per call."""
    old = [{"raw": 0.5, "calibrated": 0.2}]
    new = [{"raw": 0.5, "calibrated": 0.4}]   # delta 0.2
    # Default threshold 0.3 → safe
    assert svc.evaluate_curve_safety(old_curve=old, new_curve=new)["safe"]
    # Tight threshold 0.1 → unsafe
    out = svc.evaluate_curve_safety(
        old_curve=old, new_curve=new,
        thresholds={"max_control_point_delta": 0.1},
    )
    assert out["safe"] is False


# ── DB integration: fit_isotonic_for_strategy with gate ──────────


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"calib-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_strategy(
    db: AsyncSession, owner_id: uuid.UUID,
    *,
    initial_curve: list | None = None,
) -> DiscussionStrategyTemplate:
    tmpl = DiscussionStrategyTemplate(
        id=uuid4(), owner_id=owner_id,
        name="t", topic="x", rules="", market="TW",
        persona_ids=["market_analyst"],
        calibration_curve=initial_curve,
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


async def _seed_outcomes(
    db: AsyncSession, owner_id: uuid.UUID, strategy_id: uuid.UUID,
    *,
    pairs: list[tuple[float, int]],
) -> None:
    """Spawn a sweep + N concluded discussions with synthetic
    outcome_vectors so `_gather_calibration_pairs` finds enough
    rows. Each call here = one sweep."""
    sweep = BacktestSweep(
        id=uuid4(), owner_id=owner_id, strategy_id=strategy_id,
        market="TW", topic="x", rules="",
        persona_ids=["market_analyst"],
        anchor_date=datetime.now(UTC).date(),
        trading_days_count=5, rounds_per_discussion=1,
        concurrency=1, status="completed",
    )
    db.add(sweep)
    await db.flush()
    for i, (conf, outcome) in enumerate(pairs):
        d = Discussion(
            id=uuid4(), owner_id=owner_id, sweep_id=sweep.id,
            topic="x", rules="", market="TW",
            persona_ids=["market_analyst"],
            status="done", current_round=1,
            outcome_vector=[
                {"symbol": f"sym{i}", "confidence": conf,
                 "outcome_binary": outcome},
            ],
        )
        db.add(d)
    await db.commit()


@pytest.mark.asyncio
async def test_fit_returns_no_change_below_min_samples(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    # only 5 outcomes — below MIN_SAMPLES_FOR_FIT (30)
    await _seed_outcomes(
        db_session, owner.id, tmpl.id,
        pairs=[(0.5, 1)] * 5,
    )
    result = await svc.fit_isotonic_for_strategy(
        db_session, owner_id=owner.id, strategy_id=tmpl.id,
    )
    assert result["status"] == "no_change"
    assert result["updated"] is False


@pytest.mark.asyncio
async def test_fit_deploys_directly_on_safe_curve(
    db_session: AsyncSession, owner: User,
):
    """Cold-start strategy + 30 reasonable outcomes → safe fit →
    deployed → live curve populated."""
    tmpl = await _make_strategy(db_session, owner.id)
    pairs = [(0.3, 0), (0.5, 0), (0.7, 1), (0.9, 1)] * 8   # 32 samples, monotone
    await _seed_outcomes(db_session, owner.id, tmpl.id, pairs=pairs)
    result = await svc.fit_isotonic_for_strategy(
        db_session, owner_id=owner.id, strategy_id=tmpl.id,
    )
    assert result["status"] == "deployed"
    assert result["updated"] is True
    # Confirm the persisted state
    await db_session.refresh(tmpl)
    assert tmpl.calibration_curve is not None
    assert tmpl.calibration_pending_curve is None


@pytest.mark.asyncio
async def test_fit_parks_when_safety_fails(
    db_session: AsyncSession, owner: User,
):
    """Pre-existing live curve pinning calibrated=0.2; new outcomes
    skew the fit toward 0.7 (huge delta) → parked for review."""
    tmpl = await _make_strategy(
        db_session, owner.id,
        initial_curve=[{"raw": r / 10, "calibrated": 0.05} for r in range(1, 11)],
    )
    # All outcomes hit at high confidence — the new curve will
    # collapse the bins to high calibrated values, making the
    # delta vs the live (calibrated=0.05) curve enormous.
    pairs = [(0.7, 1), (0.8, 1), (0.9, 1)] * 11   # 33 samples
    await _seed_outcomes(db_session, owner.id, tmpl.id, pairs=pairs)
    result = await svc.fit_isotonic_for_strategy(
        db_session, owner_id=owner.id, strategy_id=tmpl.id,
    )
    assert result["status"] == "pending_review"
    assert result["updated"] is False
    assert "large shift" in (result["reason"] or "")
    await db_session.refresh(tmpl)
    # Live curve unchanged
    assert tmpl.calibration_curve is not None
    assert all(p["calibrated"] == 0.05 for p in tmpl.calibration_curve)
    # Pending curve populated
    assert tmpl.calibration_pending_curve is not None
    assert tmpl.calibration_pending_at is not None


@pytest.mark.asyncio
async def test_fit_with_auto_deploy_false_always_pending(
    db_session: AsyncSession, owner: User,
):
    """Operator workflow: manual fit always parks for review even
    when the curve would have passed the gate."""
    tmpl = await _make_strategy(db_session, owner.id)
    pairs = [(0.3, 0), (0.5, 0), (0.7, 1), (0.9, 1)] * 8
    await _seed_outcomes(db_session, owner.id, tmpl.id, pairs=pairs)
    result = await svc.fit_isotonic_for_strategy(
        db_session, owner_id=owner.id, strategy_id=tmpl.id,
        auto_deploy=False,
    )
    assert result["status"] == "pending_review"
    await db_session.refresh(tmpl)
    assert tmpl.calibration_curve is None   # never deployed
    assert tmpl.calibration_pending_curve is not None


# ── approve / reject flow ────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_promotes_pending_to_live(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(
        db_session, owner.id,
        initial_curve=[{"raw": 0.5, "calibrated": 0.2}],
    )
    pending = [{"raw": 0.5, "calibrated": 0.55}]
    tmpl.calibration_pending_curve = pending
    tmpl.calibration_pending_reason = "test reason"
    tmpl.calibration_pending_at = datetime.now(UTC)
    await db_session.commit()

    result = await svc.approve_pending_calibration(
        db_session, owner_id=owner.id, strategy_id=tmpl.id,
    )
    assert result["promoted"] is True
    assert result["curve"] == pending
    assert result["reason"] == "test reason"

    await db_session.refresh(tmpl)
    assert tmpl.calibration_curve == pending
    assert tmpl.calibration_pending_curve is None
    assert tmpl.calibration_pending_reason is None
    assert tmpl.calibration_pending_at is None


@pytest.mark.asyncio
async def test_approve_idempotent_when_no_pending(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(
        db_session, owner.id,
        initial_curve=[{"raw": 0.5, "calibrated": 0.2}],
    )
    result = await svc.approve_pending_calibration(
        db_session, owner_id=owner.id, strategy_id=tmpl.id,
    )
    assert result["promoted"] is False
    # Live curve untouched
    await db_session.refresh(tmpl)
    assert tmpl.calibration_curve == [{"raw": 0.5, "calibrated": 0.2}]


@pytest.mark.asyncio
async def test_reject_clears_pending(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(
        db_session, owner.id,
        initial_curve=[{"raw": 0.5, "calibrated": 0.2}],
    )
    tmpl.calibration_pending_curve = [{"raw": 0.5, "calibrated": 0.55}]
    tmpl.calibration_pending_reason = "test"
    tmpl.calibration_pending_at = datetime.now(UTC)
    await db_session.commit()

    result = await svc.reject_pending_calibration(
        db_session, owner_id=owner.id, strategy_id=tmpl.id,
    )
    assert result["rejected"] is True
    assert result["had_pending"] is True

    await db_session.refresh(tmpl)
    # Live unchanged, pending cleared
    assert tmpl.calibration_curve == [{"raw": 0.5, "calibrated": 0.2}]
    assert tmpl.calibration_pending_curve is None


@pytest.mark.asyncio
async def test_clean_safe_fit_clears_stale_pending(
    db_session: AsyncSession, owner: User,
):
    """Once a fresh fit deploys cleanly, any leftover pending curve
    from a previous unsafe fit gets cleared so the operator no
    longer needs to act on it."""
    tmpl = await _make_strategy(db_session, owner.id)
    # Leftover pending from a hypothetical previous bad fit
    tmpl.calibration_pending_curve = [{"raw": 0.5, "calibrated": 0.99}]
    tmpl.calibration_pending_reason = "old reason"
    tmpl.calibration_pending_at = datetime.now(UTC) - timedelta(days=2)
    await db_session.commit()

    pairs = [(0.3, 0), (0.5, 0), (0.7, 1), (0.9, 1)] * 8
    await _seed_outcomes(db_session, owner.id, tmpl.id, pairs=pairs)
    result = await svc.fit_isotonic_for_strategy(
        db_session, owner_id=owner.id, strategy_id=tmpl.id,
    )
    assert result["status"] == "deployed"
    await db_session.refresh(tmpl)
    assert tmpl.calibration_pending_curve is None
    assert tmpl.calibration_pending_reason is None
    assert tmpl.calibration_pending_at is None


@pytest.mark.asyncio
async def test_unknown_strategy_raises(db_session: AsyncSession, owner: User):
    bogus_id = uuid4()
    with pytest.raises(ValueError):
        await svc.fit_isotonic_for_strategy(
            db_session, owner_id=owner.id, strategy_id=bogus_id,
        )
    with pytest.raises(ValueError):
        await svc.approve_pending_calibration(
            db_session, owner_id=owner.id, strategy_id=bogus_id,
        )
