"""PR-5c: tests for `compare_strategies` + `compare_versions`."""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.strategy_health_metric import StrategyHealthMetric
from models.strategy_version import StrategyVersion
from models.user import User, UserRole
from services import strategy_comparison_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"sc-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_strategy(
    db: AsyncSession, owner_id: uuid.UUID,
    *, name: str = "t", market: str = "TW",
) -> DiscussionStrategyTemplate:
    tmpl = DiscussionStrategyTemplate(
        id=uuid4(), owner_id=owner_id,
        name=name, topic="x", rules="", market=market,
        persona_ids=["a"],
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


# ── compare_strategies ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_strategy_ids_returns_empty(
    db_session: AsyncSession, owner: User,
):
    out = await svc.compare_strategies(
        db_session, owner_id=owner.id, strategy_ids=[],
    )
    assert out["strategies"] == []


@pytest.mark.asyncio
async def test_truncates_to_max_4(db_session: AsyncSession, owner: User):
    """Pass 6 strategies → only first 4 returned."""
    ids = []
    for i in range(6):
        tmpl = await _make_strategy(db_session, owner.id, name=f"s{i}")
        ids.append(tmpl.id)
    out = await svc.compare_strategies(
        db_session, owner_id=owner.id, strategy_ids=ids,
    )
    assert len(out["strategies"]) == 4


@pytest.mark.asyncio
async def test_drops_strategies_not_owned(
    db_session: AsyncSession, owner: User,
):
    """A strategy owned by someone else doesn't leak through —
    response is silently shorter."""
    other = User(
        email=f"oth-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)
    a = await _make_strategy(db_session, owner.id, name="ours")
    b = await _make_strategy(db_session, other.id, name="not-ours")
    out = await svc.compare_strategies(
        db_session, owner_id=owner.id,
        strategy_ids=[a.id, b.id],
    )
    assert len(out["strategies"]) == 1
    assert out["strategies"][0]["name"] == "ours"


@pytest.mark.asyncio
async def test_includes_metric_series_and_summary(
    db_session: AsyncSession, owner: User,
):
    """Seeded snapshots → metric series + summary KPIs."""
    tmpl = await _make_strategy(db_session, owner.id)
    today = date.today()
    for offset, brier, hit in [(3, 0.20, 0.55), (2, 0.18, 0.60), (1, 0.16, 0.65)]:
        db_session.add(StrategyHealthMetric(
            strategy_id=tmpl.id,
            snapshot_date=today - timedelta(days=offset),
            brier_30d=brier,
            hit_rate_30d=hit,
            sample_count_30d=10,
            status_flags=[],
        ))
    await db_session.commit()

    out = await svc.compare_strategies(
        db_session, owner_id=owner.id, strategy_ids=[tmpl.id],
    )
    s = out["strategies"][0]
    assert len(s["metrics"]) == 3
    assert s["summary"]["brier_avg"] == pytest.approx(0.18, abs=1e-3)
    assert s["summary"]["brier_latest"] == 0.16   # most recent
    assert s["summary"]["sample_total"] == 30


# ── compare_versions ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compare_versions_weights_diff(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    v1 = StrategyVersion(
        id=uuid4(), strategy_id=tmpl.id,
        version_number=1, artifact_kind="weights",
        payload={"buffett": 1.0, "lynch": 0.5},
        sample_count=20, status="superseded",
        fit_at=datetime.now(UTC) - timedelta(days=1),
    )
    v2 = StrategyVersion(
        id=uuid4(), strategy_id=tmpl.id,
        version_number=2, artifact_kind="weights",
        payload={"buffett": 1.5, "lynch": 0.3, "soros": 0.8},
        sample_count=30, status="active",
        fit_at=datetime.now(UTC),
    )
    db_session.add_all([v1, v2])
    await db_session.commit()

    out = await svc.compare_versions(
        db_session, owner_id=owner.id, strategy_id=tmpl.id,
        v1_id=v1.id, v2_id=v2.id,
    )
    diff = out["weight_diff"]
    assert diff["buffett"]["delta"] == pytest.approx(0.5)
    assert diff["lynch"]["delta"] == pytest.approx(-0.2)
    # New persona present in v2 only → v1 None
    assert diff["soros"]["v1"] is None
    assert diff["soros"]["v2"] == 0.8


@pytest.mark.asyncio
async def test_compare_versions_curve_overlay(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    v1 = StrategyVersion(
        id=uuid4(), strategy_id=tmpl.id,
        version_number=1, artifact_kind="calibration_curve",
        payload=[{"raw": 0.3, "calibrated": 0.2}],
        status="superseded",
        fit_at=datetime.now(UTC) - timedelta(days=1),
    )
    v2 = StrategyVersion(
        id=uuid4(), strategy_id=tmpl.id,
        version_number=2, artifact_kind="calibration_curve",
        payload=[{"raw": 0.3, "calibrated": 0.25}],
        status="active",
        fit_at=datetime.now(UTC),
    )
    db_session.add_all([v1, v2])
    await db_session.commit()
    out = await svc.compare_versions(
        db_session, owner_id=owner.id, strategy_id=tmpl.id,
        v1_id=v1.id, v2_id=v2.id,
    )
    assert "curve_v1" in out
    assert "curve_v2" in out
    assert out["curve_v1"][0]["calibrated"] == 0.2
    assert out["curve_v2"][0]["calibrated"] == 0.25


@pytest.mark.asyncio
async def test_compare_versions_kind_mismatch_raises(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    v1 = StrategyVersion(
        id=uuid4(), strategy_id=tmpl.id,
        version_number=1, artifact_kind="weights",
        payload={"a": 1.0}, status="active",
        fit_at=datetime.now(UTC),
    )
    v2 = StrategyVersion(
        id=uuid4(), strategy_id=tmpl.id,
        version_number=1, artifact_kind="calibration_curve",
        payload=[], status="active",
        fit_at=datetime.now(UTC),
    )
    db_session.add_all([v1, v2])
    await db_session.commit()
    with pytest.raises(ValueError):
        await svc.compare_versions(
            db_session, owner_id=owner.id, strategy_id=tmpl.id,
            v1_id=v1.id, v2_id=v2.id,
        )


@pytest.mark.asyncio
async def test_compare_versions_unknown_strategy_raises(
    db_session: AsyncSession, owner: User,
):
    with pytest.raises(ValueError):
        await svc.compare_versions(
            db_session, owner_id=owner.id, strategy_id=uuid4(),
            v1_id=uuid4(), v2_id=uuid4(),
        )
