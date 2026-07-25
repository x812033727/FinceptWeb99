"""PR-4b: tests for the daily strategy health monitor cron.

Mocks Redis lock + notification dispatch so the test runs purely
against the SQLite test DB.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion import Discussion
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
async def test_transition_alert_writes_alert_event_row(
    db_session: AsyncSession, owner: User,
):
    """PR-D4: a healthy→degraded transition also lands one
    kind='strategy_health' row in alert_events for the owner."""
    from sqlalchemy import select

    from models.alert import AlertEvent

    tmpl = await _make_strategy(db_session, owner.id, name="degrading")
    with patch.object(cron, "acquire_lock", AsyncMock(return_value=True)), \
         patch.object(cron, "release_lock", AsyncMock()), \
         patch.object(cron, "notify_user", AsyncMock()):
        out = await cron.run_health_monitor()
    assert out["alerts_fired"] == 1

    events = list((await db_session.scalars(
        select(AlertEvent).where(AlertEvent.user_id == owner.id)
    )).all())
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "strategy_health"
    assert ev.alert_id is None
    assert ev.symbol == "degrading"
    assert ev.market == "TW"
    assert "degrading" in ev.message
    assert ev.payload["strategy_id"] == str(tmpl.id)
    assert "low_sample" in ev.payload["status_flags"]


@pytest.mark.asyncio
async def test_no_realert_while_still_degraded(
    db_session: AsyncSession, owner: User,
):
    """Second run with the strategy still degraded must NOT re-alert:
    only the healthy→degraded transition fires."""
    from sqlalchemy import select

    from models.alert import AlertEvent

    await _make_strategy(db_session, owner.id, name="sticky")
    notify = AsyncMock()
    with patch.object(cron, "acquire_lock", AsyncMock(return_value=True)), \
         patch.object(cron, "release_lock", AsyncMock()), \
         patch.object(cron, "notify_user", notify):
        first = await cron.run_health_monitor()
        second = await cron.run_health_monitor()

    assert first["alerts_fired"] == 1
    assert second["alerts_fired"] == 0
    assert notify.await_count == 1
    events = list((await db_session.scalars(select(AlertEvent))).all())
    assert len(events) == 1


@pytest.mark.asyncio
async def test_no_alert_when_previous_snapshot_already_degraded(
    db_session: AsyncSession, owner: User,
):
    """A pre-existing degraded snapshot (yesterday) suppresses the
    alert even on this pod's first run — state lives in the health
    table, not in process memory."""
    from datetime import date

    from models.strategy_health_metric import StrategyHealthMetric

    tmpl = await _make_strategy(db_session, owner.id, name="known-bad")
    db_session.add(StrategyHealthMetric(
        strategy_id=tmpl.id,
        snapshot_date=date.today() - timedelta(days=1),
        sample_count_30d=0,
        status_flags=["low_sample"],
    ))
    await db_session.commit()

    notify = AsyncMock()
    with patch.object(cron, "acquire_lock", AsyncMock(return_value=True)), \
         patch.object(cron, "release_lock", AsyncMock()), \
         patch.object(cron, "notify_user", notify):
        out = await cron.run_health_monitor()

    assert out["snapshots_written"] == 1
    assert out["alerts_fired"] == 0
    notify.assert_not_awaited()


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


async def _live_price_signal_discussion(
    db: AsyncSession, owner: User, *, verdict: str,
) -> Discussion:
    """A live (as_of_date NULL) auto-run price_signal discussion —
    the exact shape the veto guard's revert-trigger query scans."""
    d = Discussion(
        id=uuid4(),
        owner_id=owner.id,
        topic="t",
        rules="r",
        market="TW",
        status="done",
        persona_ids=[],
        auto_run=True,
        auto_run_strategy="price_signal",
        auto_run_date=date(2026, 7, 20),
        auto_run_sequence=1,
        verdict=verdict,
    )
    db.add(d)
    await db.flush()
    return d


async def _live_discussion(
    db: AsyncSession, owner: User, *,
    strategy: str, verdict: str, created_at: datetime,
) -> Discussion:
    """A live (as_of_date NULL) auto-run discussion for an arbitrary
    strategy/verdict/timestamp — used to populate the leakage watch's
    current (last 14d) vs. baseline (prior 30d) windows."""
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
        auto_run_date=created_at.date(),
        auto_run_sequence=1,
        verdict=verdict,
    )
    db.add(d)
    await db.flush()
    d.created_at = created_at
    await db.flush()
    return d


@pytest.mark.asyncio
async def test_veto_guard_revert_trigger_forces_not_ok(
    db_session: AsyncSession, owner: User,
):
    """3 consecutive live price_signal losses trips the revert guard
    (spec Part 1) independent of the per-strategy sweep — no template
    is configured here at all. `run_health_monitor` must surface the
    finding, and `health_monitor_job` must fold it into ok=False plus
    the recorded error text so it isn't a silent green."""
    for _ in range(3):
        await _live_price_signal_discussion(db_session, owner, verdict="loss")
    await db_session.commit()

    with patch.object(cron, "acquire_lock", AsyncMock(return_value=True)), \
         patch.object(cron, "release_lock", AsyncMock()):
        out = await cron.run_health_monitor()

    assert out["guard_findings"]
    assert any("revert" in f.lower() for f in out["guard_findings"])

    record_health = AsyncMock()
    with patch.object(cron, "run_health_monitor", AsyncMock(return_value=out)), \
         patch.object(cron, "record_health", record_health):
        await cron.health_monitor_job()

    assert record_health.await_args.kwargs["ok"] is False
    assert "veto_guard" in record_health.await_args.kwargs["error"]


@pytest.mark.asyncio
async def test_veto_guard_quiet_by_default(
    db_session: AsyncSession, owner: User,
):
    """Zero-config at adoption time: with no price_signal discussions
    at all (the common pre-adoption state), the guard produces no
    findings and doesn't affect ok."""
    await _make_strategy(db_session, owner.id)

    with patch.object(cron, "acquire_lock", AsyncMock(return_value=True)), \
         patch.object(cron, "release_lock", AsyncMock()), \
         patch.object(cron, "notify_user", AsyncMock()):
        out = await cron.run_health_monitor()

    assert out["guard_findings"] == []
    assert out["errors"] == 0


@pytest.mark.asyncio
async def test_veto_guard_leakage_fires_and_ignores_unverifiable(
    db_session: AsyncSession, owner: User,
):
    """chip_quality's abstain rate collapsing >20pp from baseline to
    current trips the leakage watch. `unverifiable` rows salted into
    the baseline window must NOT dilute the rate (Finding 1's fix:
    `unverifiable` is a data-availability bucket excluded from the
    denominator, same convention as `daily_scoreboard_service`'s
    win-rate calc) — if they did, the baseline rate would read 62.5%
    instead of 100% and this assertion on the exact percentage would
    catch it."""
    now = datetime.now(UTC)
    current_ts = now - timedelta(days=1)
    baseline_ts = now - timedelta(days=20)

    for _ in range(5):
        await _live_discussion(
            db_session, owner, strategy="chip_quality",
            verdict="abstain", created_at=baseline_ts,
        )
    for _ in range(3):
        await _live_discussion(
            db_session, owner, strategy="chip_quality",
            verdict="unverifiable", created_at=baseline_ts,
        )
    for _ in range(5):
        await _live_discussion(
            db_session, owner, strategy="chip_quality",
            verdict="win", created_at=current_ts,
        )
    await db_session.commit()

    with patch.object(cron, "acquire_lock", AsyncMock(return_value=True)), \
         patch.object(cron, "release_lock", AsyncMock()):
        out = await cron.run_health_monitor()

    findings = out["guard_findings"]
    assert findings
    finding = next(f for f in findings if "chip_quality" in f)
    assert "from 100%" in finding   # baseline rate unchanged by the 3 unverifiable rows
    assert "to 0% (last 14d)" in finding   # current rate — not just a substring of "100%"

    record_health = AsyncMock()
    with patch.object(cron, "run_health_monitor", AsyncMock(return_value=out)), \
         patch.object(cron, "record_health", record_health):
        await cron.health_monitor_job()

    assert record_health.await_args.kwargs["ok"] is False
    assert "chip_quality" in record_health.await_args.kwargs["error"]


@pytest.mark.asyncio
async def test_veto_guard_leakage_skips_thin_window(
    db_session: AsyncSession, owner: User,
):
    """Noise floor: a window with fewer than 5 verdicts is skipped
    entirely, even though the raw rates would otherwise read as a
    dramatic (and spurious) drop."""
    now = datetime.now(UTC)
    current_ts = now - timedelta(days=1)
    baseline_ts = now - timedelta(days=20)

    for _ in range(5):
        await _live_discussion(
            db_session, owner, strategy="chip_quality",
            verdict="abstain", created_at=baseline_ts,
        )
    for _ in range(3):   # below the 5-sample noise floor
        await _live_discussion(
            db_session, owner, strategy="chip_quality",
            verdict="win", created_at=current_ts,
        )
    await db_session.commit()

    with patch.object(cron, "acquire_lock", AsyncMock(return_value=True)), \
         patch.object(cron, "release_lock", AsyncMock()):
        out = await cron.run_health_monitor()

    assert out["guard_findings"] == []


@pytest.mark.asyncio
async def test_veto_guard_leakage_quiet_when_rate_rises(
    db_session: AsyncSession, owner: User,
):
    """A rise in abstain rate (current high, baseline low) must NOT
    fire — it's the opposite of leakage. This also catches a
    current/baseline window-swap bug: if the current-window query were
    accidentally given the baseline date range (or vice versa), this
    exact data would flip into a spurious 100pp "drop" and fire."""
    now = datetime.now(UTC)
    current_ts = now - timedelta(days=1)
    baseline_ts = now - timedelta(days=20)

    for _ in range(5):
        await _live_discussion(
            db_session, owner, strategy="chip_quality",
            verdict="win", created_at=baseline_ts,   # baseline: 0% abstain
        )
    for _ in range(5):
        await _live_discussion(
            db_session, owner, strategy="chip_quality",
            verdict="abstain", created_at=current_ts,   # current: 100% abstain
        )
    await db_session.commit()

    with patch.object(cron, "acquire_lock", AsyncMock(return_value=True)), \
         patch.object(cron, "release_lock", AsyncMock()):
        out = await cron.run_health_monitor()

    assert out["guard_findings"] == []
