"""Tests for `services.confidence_calibrator` (PR-C2).

The PAV core is a pure function so most tests don't need a DB.
The strategy-level fit (`fit_isotonic_for_strategy`) is tested
against an in-memory SQLite fixture with seeded sweep + discussion
rows carrying outcome_vector data (the surface PR-C1 writes).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion import Discussion
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.user import User, UserRole
from services import confidence_calibrator as cal


# ── PAV core (pure-function) ─────────────────────────────────────


def test_pav_already_monotone_passes_through():
    """When the underlying data is already monotone non-decreasing,
    PAV returns one block per unique x with the per-bucket mean."""
    pairs = [
        (0.3, 0), (0.3, 0), (0.3, 0), (0.3, 1),
        (0.7, 1), (0.7, 1), (0.7, 0), (0.7, 0),
        (0.9, 1), (0.9, 1), (0.9, 1), (0.9, 0),
    ]
    curve = cal.fit_isotonic_pav(pairs)
    # Three unique x → three control points, monotone:
    # 0.3 → 0.25, 0.7 → 0.5, 0.9 → 0.75
    assert len(curve) == 3
    xs = [p[0] for p in curve]
    ys = [p[1] for p in curve]
    assert xs == [0.3, 0.7, 0.9]
    assert ys[0] < ys[1] < ys[2]
    assert ys[0] == pytest.approx(0.25)
    assert ys[2] == pytest.approx(0.75)


def test_pav_merges_violating_pair():
    """Synthesizer is over-confident at 0.9 (only 30% hit rate)
    while genuine at 0.7 (50% hit rate). PAV merges the two
    blocks into a single 0.9 bucket with the pooled mean."""
    pairs = (
        [(0.7, 1)] * 5 + [(0.7, 0)] * 5      # mean 0.5 at 0.7
        + [(0.9, 1)] * 3 + [(0.9, 0)] * 7    # mean 0.3 at 0.9 — VIOLATION
    )
    curve = cal.fit_isotonic_pav(pairs)
    assert len(curve) == 1
    # Pooled mean = (5+3) / 20 = 0.4. Bucket x = 0.9 (rightmost).
    assert curve[0][0] == 0.9
    assert curve[0][1] == pytest.approx(0.4)


def test_pav_extreme_overconfidence_collapses_to_low_calibrated():
    """Synthesizer says 0.9 N times, all wrong. The curve should
    map 0.9 → 0.0 (or close), not the synthesizer's nominal 0.9."""
    pairs = [(0.9, 0)] * 20
    curve = cal.fit_isotonic_pav(pairs)
    assert len(curve) == 1
    assert curve[0][0] == 0.9
    assert curve[0][1] == pytest.approx(0.0)


def test_pav_returns_empty_for_empty_input():
    assert cal.fit_isotonic_pav([]) == []


def test_pav_handles_invalid_outcomes_silently():
    """Defensive: bad outcome values (not 0/1) get filtered before
    fitting rather than blowing up. Pairs with NaN outcome are
    silently dropped."""
    pairs = [(0.5, 0), (0.5, 1), (0.5, 2), (0.5, -1)]   # 2 / -1 invalid
    curve = cal.fit_isotonic_pav(pairs)
    # Only the valid pairs (0.5, 0) + (0.5, 1) contribute.
    # Mean = 0.5 at x=0.5.
    assert curve == [(0.5, 0.5)]


def test_pav_groups_same_x_into_one_block_upfront():
    """4 pairs at the same x get averaged before PAV runs (so we
    don't emit 4 duplicate control points)."""
    pairs = [(0.5, 1), (0.5, 0), (0.5, 0), (0.5, 1)]
    curve = cal.fit_isotonic_pav(pairs)
    assert curve == [(0.5, 0.5)]


def test_pav_preserves_monotonicity_globally():
    """Throw a noisy multi-bucket signal at PAV and check the output
    has no monotonicity violation."""
    pairs = []
    # Lots of buckets with random hit rates that don't fit a
    # smooth curve — PAV should still produce a monotone result.
    bucket_truths = {
        0.1: 0.05, 0.2: 0.15, 0.3: 0.25, 0.4: 0.20,   # dip!
        0.5: 0.45, 0.6: 0.55, 0.7: 0.50,              # dip again!
        0.8: 0.65, 0.9: 0.85,
    }
    for x, true_rate in bucket_truths.items():
        n = 40
        wins = int(true_rate * n)
        pairs.extend([(x, 1)] * wins + [(x, 0)] * (n - wins))
    curve = cal.fit_isotonic_pav(pairs)
    ys = [p[1] for p in curve]
    for i in range(1, len(ys)):
        assert ys[i] >= ys[i - 1] - 1e-9, (
            f"violation at {curve[i - 1]} -> {curve[i]}"
        )


# ── apply_calibration ────────────────────────────────────────────


def test_apply_calibration_uses_first_bucket_above_raw():
    curve = [
        {"raw": 0.3, "calibrated": 0.2},
        {"raw": 0.7, "calibrated": 0.5},
        {"raw": 0.9, "calibrated": 0.75},
    ]
    assert cal.apply_calibration(0.1, curve) == 0.2   # in <=0.3 bucket
    assert cal.apply_calibration(0.4, curve) == 0.5   # in 0.3-0.7
    assert cal.apply_calibration(0.7, curve) == 0.5   # boundary → upper bucket
    assert cal.apply_calibration(0.85, curve) == 0.75  # 0.7-0.9


def test_apply_calibration_clamps_when_raw_exceeds_curve():
    curve = [{"raw": 0.5, "calibrated": 0.4}]
    # raw 0.99 above the highest observed bucket → return rightmost
    assert cal.apply_calibration(0.99, curve) == 0.4


def test_apply_calibration_returns_raw_when_curve_empty():
    assert cal.apply_calibration(0.7, []) == 0.7
    assert cal.apply_calibration(0.7, None) == 0.7


def test_apply_calibration_clamps_input_to_unit_interval():
    curve = [{"raw": 1.0, "calibrated": 0.6}]
    assert cal.apply_calibration(1.5, curve) == 0.6   # clamped raw 1.0
    assert cal.apply_calibration(-0.3, curve) == 0.6   # clamped raw 0.0


def test_apply_calibration_accepts_tuple_input_shape():
    """fit_isotonic_pav emits tuples; apply must also handle that
    so we don't need a serialize round-trip at fit time."""
    curve = [(0.3, 0.2), (0.7, 0.5)]
    assert cal.apply_calibration(0.5, curve) == 0.5


def test_apply_calibration_skips_malformed_entries():
    curve: list = [
        {"raw": 0.3},          # missing calibrated → skip
        "garbage",              # wrong type → skip
        {"raw": 0.7, "calibrated": 0.5},
    ]
    # Only the valid entry remains; raw=0.6 falls into 0.7 bucket.
    assert cal.apply_calibration(0.6, curve) == 0.5


# ── fit_isotonic_for_strategy (DB-backed) ────────────────────────


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    u = User(
        email=f"cal-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


async def _make_strategy(
    db: AsyncSession, *, owner_id: UUID,
) -> DiscussionStrategyTemplate:
    tmpl = DiscussionStrategyTemplate(
        id=uuid.uuid4(),
        owner_id=owner_id,
        name="Cal Test",
        topic="t", rules="r",
        market="TW",
        persona_ids=["a"],
        default_rounds=1,
        default_concurrency=1,
        default_auto_post_mortem=False,
        persona_weights={},
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


async def _seed_sweep_with_outcomes(
    db: AsyncSession, *,
    owner_id: UUID,
    strategy_id: UUID,
    outcomes: list[tuple[float, int]],   # (confidence, outcome_binary)
) -> BacktestSweep:
    """Build one sweep + one discussion carrying the supplied
    outcome_vector. Tests can chain multiple of these to grow the
    pool above MIN_SAMPLES_FOR_FIT."""
    sweep = BacktestSweep(
        id=uuid.uuid4(),
        owner_id=owner_id,
        topic="t", rules="r", market="TW",
        persona_ids=["a"],
        anchor_date=date(2026, 1, 5),
        trading_days_count=5, rounds_per_discussion=1,
        concurrency=1, auto_post_mortem=False,
        strategy_id=strategy_id,
    )
    db.add(sweep)
    await db.commit()
    await db.refresh(sweep)
    disc = Discussion(
        id=uuid.uuid4(),
        owner_id=owner_id,
        topic="t", rules="r",
        persona_ids=["a"], market="TW",
        status="done", current_round=1,
        sweep_id=sweep.id,
        outcome_vector=[
            {
                "symbol": f"S{i}",
                "confidence": conf,
                "outcome_binary": outcome,
            }
            for i, (conf, outcome) in enumerate(outcomes)
        ],
        brier_score=0.1,   # placeholder — fit doesn't read this
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(disc)
    await db.commit()
    return sweep


@pytest.mark.asyncio
async def test_fit_below_min_samples_does_not_persist(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    # Seed 10 pairs (well below MIN_SAMPLES_FOR_FIT=30).
    await _seed_sweep_with_outcomes(
        db_session,
        owner_id=owner.id, strategy_id=tmpl.id,
        outcomes=[(0.7, 1)] * 5 + [(0.7, 0)] * 5,
    )
    out = await cal.fit_isotonic_for_strategy(
        db_session, owner_id=owner.id, strategy_id=tmpl.id,
    )
    assert out["updated"] is False
    assert out["samples"] == 10
    assert "30" in out["reason"]
    refreshed = await db_session.scalar(
        __import__(
            "sqlalchemy", fromlist=["select"],
        ).select(DiscussionStrategyTemplate).where(
            DiscussionStrategyTemplate.id == tmpl.id,
        )
    )
    assert refreshed is not None
    assert refreshed.calibration_curve in (None, [])


@pytest.mark.asyncio
async def test_fit_with_extreme_overconfidence_compresses_high_conf(
    db_session: AsyncSession, owner: User,
):
    """Build a 30-sample pool where the synthesizer was very
    overconfident at 0.9 (30% hit rate). The fitted curve should
    map raw 0.9 to ~0.3, not 0.9."""
    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    await _seed_sweep_with_outcomes(
        db_session,
        owner_id=owner.id, strategy_id=tmpl.id,
        outcomes=[(0.9, 1)] * 9 + [(0.9, 0)] * 21,
    )
    out = await cal.fit_isotonic_for_strategy(
        db_session, owner_id=owner.id, strategy_id=tmpl.id,
    )
    assert out["updated"] is True
    assert out["samples"] == 30
    curve = out["curve"]
    assert any(
        c["raw"] == 0.9 and c["calibrated"] < 0.4
        for c in curve
    ), curve

    # Verify it's persisted.
    refreshed = await db_session.scalar(
        __import__(
            "sqlalchemy", fromlist=["select"],
        ).select(DiscussionStrategyTemplate).where(
            DiscussionStrategyTemplate.id == tmpl.id,
        )
    )
    assert refreshed is not None
    assert refreshed.calibration_curve == curve
    assert refreshed.calibration_sample_count == 30
    assert refreshed.calibration_updated_at is not None


@pytest.mark.asyncio
async def test_fit_pulls_from_multiple_sweeps(
    db_session: AsyncSession, owner: User,
):
    """Pool grows across sweeps. Two sweeps × 20 pairs each = 40
    samples, above the threshold."""
    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    await _seed_sweep_with_outcomes(
        db_session,
        owner_id=owner.id, strategy_id=tmpl.id,
        outcomes=[(0.5, 1)] * 10 + [(0.5, 0)] * 10,
    )
    await _seed_sweep_with_outcomes(
        db_session,
        owner_id=owner.id, strategy_id=tmpl.id,
        outcomes=[(0.5, 1)] * 10 + [(0.5, 0)] * 10,
    )
    out = await cal.fit_isotonic_for_strategy(
        db_session, owner_id=owner.id, strategy_id=tmpl.id,
    )
    assert out["updated"] is True
    assert out["samples"] == 40
    # Single bucket → mean 0.5 (well-calibrated).
    assert len(out["curve"]) == 1
    assert out["curve"][0]["calibrated"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_fit_skips_discussions_without_outcome_vector(
    db_session: AsyncSession, owner: User,
):
    """Pre-PR-C1 discussions have NULL outcome_vector — they get
    silently skipped without polluting the curve."""
    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    # 30 valid pairs in one sweep.
    sweep = await _seed_sweep_with_outcomes(
        db_session,
        owner_id=owner.id, strategy_id=tmpl.id,
        outcomes=[(0.6, 1)] * 18 + [(0.6, 0)] * 12,
    )
    # Add a stray discussion with NULL outcome_vector.
    null_disc = Discussion(
        id=uuid.uuid4(),
        owner_id=owner.id,
        topic="t", rules="r",
        persona_ids=["a"], market="TW",
        status="done", current_round=1,
        sweep_id=sweep.id,
        outcome_vector=None,
    )
    db_session.add(null_disc)
    await db_session.commit()

    out = await cal.fit_isotonic_for_strategy(
        db_session, owner_id=owner.id, strategy_id=tmpl.id,
    )
    assert out["updated"] is True
    # Sample count comes from the valid outcome_vector entries only.
    assert out["samples"] == 30


@pytest.mark.asyncio
async def test_fit_raises_for_unknown_strategy(
    db_session: AsyncSession, owner: User,
):
    with pytest.raises(ValueError, match="strategy template"):
        await cal.fit_isotonic_for_strategy(
            db_session,
            owner_id=owner.id,
            strategy_id=uuid.uuid4(),
        )
