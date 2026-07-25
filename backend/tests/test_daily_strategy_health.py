"""The daily roundtable's health metrics.

The bug these guard: `strategy_health_metrics` held 0 rows for the life
of the deployment because every sampling query reached discussions
through `BacktestSweep.strategy_id -> Discussion.sweep_id`, and auto-run
discussions have `sweep_id` NULL. The job reported healthy every
morning while doing nothing.
"""
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.user import User, UserRole
from services import strategy_health_service as hsvc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"dailyhealth-{uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _template(db: AsyncSession, owner: User, *, auto_run_strategy):
    tmpl = DiscussionStrategyTemplate(
        owner_id=owner.id,
        auto_run_strategy=auto_run_strategy,
        name="t",
        topic="topic",
        rules="rules",
        market="TW",
        persona_ids=[],
    )
    db.add(tmpl)
    await db.flush()
    return tmpl


async def _auto_run_discussion(
    db: AsyncSession, owner: User, *, strategy: str,
    brier: float | None = 0.2, verdict: str | None = "win",
    created_at: datetime | None = None,
):
    d = Discussion(
        id=uuid4(),
        owner_id=owner.id,
        topic="t",
        rules="r",
        market="TW",
        status="done",
        persona_ids=[],
        auto_run=True,
        auto_run_strategy=strategy,
        auto_run_date=date(2026, 7, 20),
        auto_run_sequence=1,
        brier_score=brier,
        verdict=verdict,
    )
    db.add(d)
    await db.flush()
    if created_at is not None:
        d.created_at = created_at
        await db.flush()
    return d


@pytest.mark.asyncio
async def test_auto_run_template_samples_its_own_discussions(
    db_session: AsyncSession, owner: User,
):
    """The whole point: no sweep exists, and samples still land."""
    tmpl = await _template(db_session, owner, auto_run_strategy="general")
    await _auto_run_discussion(db_session, owner, strategy="general", brier=0.2)
    await _auto_run_discussion(db_session, owner, strategy="general", brier=0.4)

    raw, _ = await hsvc._gather_recent_briers(
        db_session, tmpl.id, snapshot=datetime.now(UTC).date(),
    )
    assert sorted(raw) == [0.2, 0.4]


@pytest.mark.asyncio
async def test_auto_run_template_ignores_other_strategies(
    db_session: AsyncSession, owner: User,
):
    """Three strategies share the auto-run flag, so the strategy key
    has to actually filter — otherwise every template reports the same
    blended number and 'which strategy is drifting' is unanswerable."""
    tmpl = await _template(db_session, owner, auto_run_strategy="general")
    await _auto_run_discussion(db_session, owner, strategy="general", brier=0.2)
    await _auto_run_discussion(
        db_session, owner, strategy="chip_quality", brier=0.9,
    )

    raw, _ = await hsvc._gather_recent_briers(
        db_session, tmpl.id, snapshot=datetime.now(UTC).date(),
    )
    assert raw == [0.2]


@pytest.mark.asyncio
async def test_hit_rate_uses_the_same_scope(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _template(db_session, owner, auto_run_strategy="price_signal")
    await _auto_run_discussion(
        db_session, owner, strategy="price_signal", verdict="win",
    )
    await _auto_run_discussion(
        db_session, owner, strategy="price_signal", verdict="loss",
    )
    await _auto_run_discussion(
        db_session, owner, strategy="general", verdict="win",
    )

    rate, n = await hsvc._gather_window_hit_rate(
        db_session, tmpl.id, snapshot=datetime.now(UTC).date(),
    )
    assert (rate, n) == (0.5, 2)


@pytest.mark.asyncio
async def test_rolling_window_still_bounds_auto_run_samples(
    db_session: AsyncSession, owner: User,
):
    """The scope swap must not accidentally drop the 30-day window —
    an unbounded scope would quietly turn a rolling metric into an
    all-time one."""
    tmpl = await _template(db_session, owner, auto_run_strategy="general")
    await _auto_run_discussion(db_session, owner, strategy="general", brier=0.2)
    await _auto_run_discussion(
        db_session, owner, strategy="general", brier=0.9,
        created_at=datetime.now(UTC) - timedelta(days=90),
    )

    raw, _ = await hsvc._gather_recent_briers(
        db_session, tmpl.id, snapshot=datetime.now(UTC).date(),
    )
    assert raw == [0.2]


@pytest.mark.asyncio
async def test_sweep_templates_are_unaffected(
    db_session: AsyncSession, owner: User,
):
    """A template with no `auto_run_strategy` and no sweeps must still
    resolve to 'no samples' rather than picking up every auto-run
    discussion in the deployment."""
    tmpl = await _template(db_session, owner, auto_run_strategy=None)
    await _auto_run_discussion(db_session, owner, strategy="general", brier=0.2)

    raw, _ = await hsvc._gather_recent_briers(
        db_session, tmpl.id, snapshot=datetime.now(UTC).date(),
    )
    assert raw == []


@pytest.mark.asyncio
async def test_snapshot_lands_a_row_for_an_auto_run_strategy(
    db_session: AsyncSession, owner: User,
):
    """End-to-end for the acceptance check: `strategy_health_metrics`
    goes from 0 rows to a real row with a non-zero sample count."""
    from models.strategy_health_metric import StrategyHealthMetric

    tmpl = await _template(db_session, owner, auto_run_strategy="general")
    for b in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6):
        await _auto_run_discussion(
            db_session, owner, strategy="general", brier=b,
        )

    row = await hsvc.record_snapshot(db_session, strategy_id=tmpl.id)
    assert row.sample_count_30d == 6
    assert row.brier_30d is not None

    stored = await db_session.scalar(
        select(StrategyHealthMetric).where(
            StrategyHealthMetric.strategy_id == tmpl.id,
        )
    )
    assert stored is not None
