"""PR-5a: tests for `compose_timeline` — the multi-source event +
metric assembler that powers the StrategyTimelineCard.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.strategy_health_metric import StrategyHealthMetric
from models.strategy_version import StrategyVersion
from models.user import User, UserRole
from services import strategy_timeline_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"tl-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_strategy(
    db: AsyncSession, owner_id: uuid.UUID,
    *,
    persona_status: dict[str, str] | None = None,
    persona_status_updated_at: datetime | None = None,
) -> DiscussionStrategyTemplate:
    tmpl = DiscussionStrategyTemplate(
        id=uuid4(), owner_id=owner_id,
        name="t", topic="x", rules="", market="TW",
        persona_ids=["a", "b"],
        persona_status=persona_status or {},
        persona_status_updated_at=persona_status_updated_at,
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


# ── empty / cold-start ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_strategy_returns_empty(db_session: AsyncSession):
    out = await svc.compose_timeline(db_session, strategy_id=uuid4())
    assert out["metrics"] == []
    assert out["events"] == []


@pytest.mark.asyncio
async def test_cold_strategy_no_events(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    out = await svc.compose_timeline(db_session, strategy_id=tmpl.id)
    assert out["metrics"] == []
    assert out["events"] == []
    assert out["strategy_id"] == str(tmpl.id)


# ── metric series ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_in_chronological_order(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    today = date.today()
    for offset in (10, 5, 1):
        db_session.add(StrategyHealthMetric(
            strategy_id=tmpl.id,
            snapshot_date=today - timedelta(days=offset),
            brier_30d=0.20 + offset * 0.01,
            sample_count_30d=10,
            status_flags=[],
        ))
    await db_session.commit()

    out = await svc.compose_timeline(db_session, strategy_id=tmpl.id, days=30)
    assert len(out["metrics"]) == 3
    # Oldest first (chronological)
    dates = [m["date"] for m in out["metrics"]]
    assert dates == sorted(dates)


# ── version events ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_version_event_trigger_detected_from_notes(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    now = datetime.now(UTC)
    # Two version rows with different note styles
    db_session.add_all([
        StrategyVersion(
            id=uuid4(), strategy_id=tmpl.id,
            version_number=1, artifact_kind="weights",
            payload={"a": 1.0}, fit_at=now - timedelta(hours=2),
            status="superseded",
            notes="auto-promoted from walk-forward test fold; brier=0.18",
        ),
        StrategyVersion(
            id=uuid4(), strategy_id=tmpl.id,
            version_number=2, artifact_kind="weights",
            payload={"a": 1.5}, fit_at=now - timedelta(hours=1),
            status="active",
            notes="rolled back from v1",
        ),
        StrategyVersion(
            id=uuid4(), strategy_id=tmpl.id,
            version_number=3, artifact_kind="calibration_curve",
            payload=[{"raw": 0.5, "calibrated": 0.4}],
            fit_at=now,
            status="active",
            notes="approved from pending; original reason: large shift",
        ),
    ])
    await db_session.commit()

    out = await svc.compose_timeline(db_session, strategy_id=tmpl.id)
    triggers = [e["trigger"] for e in out["events"] if e["kind"] == "version_change"]
    assert "auto_promote" in triggers
    assert "rollback" in triggers
    assert "calibration_approved" in triggers


# ── sweep events ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_completed_event_with_counts(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    sweep = BacktestSweep(
        id=uuid4(), owner_id=owner.id, strategy_id=tmpl.id,
        market="TW", topic="x", rules="",
        persona_ids=["a"],
        anchor_date=date(2026, 4, 1),
        trading_days_count=5, rounds_per_discussion=1,
        concurrency=1, status="completed",
        fold_kind="production",
        completed_at=datetime.now(UTC) - timedelta(days=1),
        completed_dates=["2026-04-01", "2026-04-02", "2026-04-03"],
        failed_dates=[],
    )
    db_session.add(sweep)
    await db_session.commit()
    out = await svc.compose_timeline(db_session, strategy_id=tmpl.id)
    sweep_events = [e for e in out["events"] if e["kind"] == "sweep_completed"]
    assert len(sweep_events) == 1
    assert sweep_events[0]["fold_kind"] == "production"
    assert sweep_events[0]["discussions_completed"] == 3


# ── maturity transitions ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_maturity_change_event_on_tier_transition(
    db_session: AsyncSession, owner: User,
):
    """3 daily snapshots: learning → mature → drifting → 2 events."""
    tmpl = await _make_strategy(db_session, owner.id)
    today = date.today()
    for offset, tier in [(3, "learning"), (2, "mature"), (1, "drifting")]:
        db_session.add(StrategyHealthMetric(
            strategy_id=tmpl.id,
            snapshot_date=today - timedelta(days=offset),
            brier_30d=0.20, sample_count_30d=10, status_flags=[],
            maturity_tier_at_snapshot=tier,
        ))
    await db_session.commit()
    out = await svc.compose_timeline(db_session, strategy_id=tmpl.id)
    events = [e for e in out["events"] if e["kind"] == "maturity_change"]
    assert len(events) == 2
    assert events[0]["from"] == "learning"
    assert events[0]["to"] == "mature"
    assert events[1]["from"] == "mature"
    assert events[1]["to"] == "drifting"


@pytest.mark.asyncio
async def test_maturity_no_event_when_tier_unchanged(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    today = date.today()
    for offset in (3, 2, 1):
        db_session.add(StrategyHealthMetric(
            strategy_id=tmpl.id,
            snapshot_date=today - timedelta(days=offset),
            brier_30d=0.20, sample_count_30d=10, status_flags=[],
            maturity_tier_at_snapshot="mature",
        ))
    await db_session.commit()
    out = await svc.compose_timeline(db_session, strategy_id=tmpl.id)
    events = [e for e in out["events"] if e["kind"] == "maturity_change"]
    assert events == []


# ── persona status events ────────────────────────────────────────


@pytest.mark.asyncio
async def test_persona_status_event_within_window(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(
        db_session, owner.id,
        persona_status={"a": "frozen"},
        persona_status_updated_at=datetime.now(UTC) - timedelta(days=2),
    )
    out = await svc.compose_timeline(db_session, strategy_id=tmpl.id)
    events = [e for e in out["events"] if e["kind"] == "persona_status_change"]
    assert len(events) == 1
    assert events[0]["non_active_personas"] == ["a"]
    assert events[0]["non_active_count"] == 1


@pytest.mark.asyncio
async def test_persona_status_event_outside_window(
    db_session: AsyncSession, owner: User,
):
    """A persona-status update from 6 months ago shouldn't appear
    in a 90-day window."""
    tmpl = await _make_strategy(
        db_session, owner.id,
        persona_status={"a": "frozen"},
        persona_status_updated_at=datetime.now(UTC) - timedelta(days=200),
    )
    out = await svc.compose_timeline(db_session, strategy_id=tmpl.id, days=90)
    events = [e for e in out["events"] if e["kind"] == "persona_status_change"]
    assert events == []


# ── ordering across event kinds ──────────────────────────────────


@pytest.mark.asyncio
async def test_events_sorted_chronologically(
    db_session: AsyncSession, owner: User,
):
    """Events from different sources should interleave by date,
    not group by source."""
    tmpl = await _make_strategy(db_session, owner.id)
    now = datetime.now(UTC)
    today = date.today()

    # version event 3 days ago
    db_session.add(StrategyVersion(
        id=uuid4(), strategy_id=tmpl.id,
        version_number=1, artifact_kind="weights",
        payload={}, fit_at=now - timedelta(days=3),
        status="superseded", notes="manual fit",
    ))
    # sweep event 2 days ago
    db_session.add(BacktestSweep(
        id=uuid4(), owner_id=owner.id, strategy_id=tmpl.id,
        market="TW", topic="x", rules="",
        persona_ids=["a"], anchor_date=today,
        trading_days_count=1, rounds_per_discussion=1,
        concurrency=1, status="completed",
        fold_kind="production",
        completed_at=now - timedelta(days=2),
    ))
    # tier change today (need two snapshots)
    db_session.add(StrategyHealthMetric(
        strategy_id=tmpl.id,
        snapshot_date=today - timedelta(days=1),
        brier_30d=0.2, sample_count_30d=10, status_flags=[],
        maturity_tier_at_snapshot="learning",
    ))
    db_session.add(StrategyHealthMetric(
        strategy_id=tmpl.id,
        snapshot_date=today,
        brier_30d=0.2, sample_count_30d=10, status_flags=[],
        maturity_tier_at_snapshot="mature",
    ))
    await db_session.commit()

    out = await svc.compose_timeline(db_session, strategy_id=tmpl.id)
    dates = [e["date"] for e in out["events"]]
    assert dates == sorted(dates)
