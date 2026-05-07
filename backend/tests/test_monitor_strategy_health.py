"""PR-4b: tests for the daily strategy health monitor cron.

Mocks Redis lock + notification dispatch so the test runs purely
against the SQLite test DB.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.user import User, UserRole
from tasks import monitor_strategy_health as cron


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"mhc-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_strategy(
    db: AsyncSession, owner_id: uuid.UUID,
    *, deleted: bool = False, name: str = "t",
) -> DiscussionStrategyTemplate:
    tmpl = DiscussionStrategyTemplate(
        id=uuid4(), owner_id=owner_id,
        name=name, topic="x", rules="", market="TW",
        persona_ids=["a"],
        deleted_at=datetime.now(UTC) if deleted else None,
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


@pytest.mark.asyncio
async def test_skipped_when_lock_held(db_session: AsyncSession, owner: User):
    """Multi-pod safety: when another pod holds the lock, this run
    is a clean no-op (no snapshots written, no errors)."""
    await _make_strategy(db_session, owner.id)
    with patch.object(cron, "acquire_lock", AsyncMock(return_value=False)), \
         patch.object(cron, "release_lock", AsyncMock()):
        out = await cron.run_health_monitor()
    assert out["snapshots_written"] == 0
    assert out["skipped"] == "lock_held"


@pytest.mark.asyncio
async def test_writes_snapshot_per_active_strategy(
    db_session: AsyncSession, owner: User,
):
    """Two strategies: one active (gets snapshot), one soft-
    deleted (skipped — no row written)."""
    await _make_strategy(db_session, owner.id, name="active")
    await _make_strategy(db_session, owner.id, deleted=True, name="deleted")
    with patch.object(cron, "acquire_lock", AsyncMock(return_value=True)), \
         patch.object(cron, "release_lock", AsyncMock()), \
         patch.object(cron, "notify_user", AsyncMock()):
        out = await cron.run_health_monitor()
    assert out["strategies_total"] == 1
    assert out["snapshots_written"] == 1
    assert out["errors"] == 0


@pytest.mark.asyncio
async def test_skips_stale_strategy_no_snapshot_row(
    db_session: AsyncSession, owner: User,
):
    """A strategy whose maturity computes to `stale` shouldn't
    accumulate further snapshot rows — they'd just be NULL data
    bloat. The maturity tier is still updated though."""
    tmpl = await _make_strategy(db_session, owner.id)
    # Force stale: an old completed sweep, no recent ones
    sweep = BacktestSweep(
        id=uuid4(), owner_id=owner.id, strategy_id=tmpl.id,
        market="TW", topic="x", rules="",
        persona_ids=["a"],
        anchor_date=datetime.now(UTC).date(),
        trading_days_count=5, rounds_per_discussion=1,
        concurrency=1, status="completed",
        fold_kind="production",
        completed_at=datetime.now(UTC) - timedelta(days=45),
    )
    db_session.add(sweep)
    await db_session.commit()

    with patch.object(cron, "acquire_lock", AsyncMock(return_value=True)), \
         patch.object(cron, "release_lock", AsyncMock()), \
         patch.object(cron, "notify_user", AsyncMock()):
        out = await cron.run_health_monitor()
    assert out["snapshots_written"] == 0


@pytest.mark.asyncio
async def test_fires_alert_on_status_flag(
    db_session: AsyncSession, owner: User,
):
    """When a snapshot has non-empty status_flags, notify_user
    fires once for that strategy."""
    await _make_strategy(db_session, owner.id, name="alerting")
    notify = AsyncMock()
    with patch.object(cron, "acquire_lock", AsyncMock(return_value=True)), \
         patch.object(cron, "release_lock", AsyncMock()), \
         patch.object(cron, "notify_user", notify):
        out = await cron.run_health_monitor()
    # Zero-data strategy → low_sample flag fires → one alert
    assert out["alerts_fired"] == 1
    notify.assert_awaited_once()
    call_args = notify.await_args
    assert call_args.args[0] == str(owner.id)
    payload = call_args.args[1]
    assert payload["kind"] == "strategy_health_alert"
    assert payload["strategy_name"] == "alerting"
    assert "low_sample" in payload["status_flags"]


@pytest.mark.asyncio
async def test_per_strategy_failure_isolated(
    db_session: AsyncSession, owner: User,
):
    """When one strategy raises during compute, other strategies
    still get their snapshots."""
    a = await _make_strategy(db_session, owner.id, name="A")
    await _make_strategy(db_session, owner.id, name="B")

    real_record = cron.hsvc.record_snapshot

    async def _flaky_record(db, strategy_id, **kw):
        if strategy_id == a.id:
            raise RuntimeError("boom")
        return await real_record(db, strategy_id, **kw)

    with patch.object(cron, "acquire_lock", AsyncMock(return_value=True)), \
         patch.object(cron, "release_lock", AsyncMock()), \
         patch.object(cron, "notify_user", AsyncMock()), \
         patch.object(cron.hsvc, "record_snapshot", _flaky_record):
        out = await cron.run_health_monitor()
    assert out["errors"] == 1
    assert out["snapshots_written"] == 1   # only B
