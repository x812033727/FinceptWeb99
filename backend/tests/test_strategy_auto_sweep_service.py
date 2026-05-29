"""Tests for `services.strategy_auto_sweep_service` (PR-D)."""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.user import User, UserRole
from services import strategy_auto_sweep_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    u = User(
        email=f"autosched-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


def _make_template(
    owner_id: uuid.UUID,
    *,
    enabled: bool = True,
    cadence_hours: int = 24,
    last_run_at: datetime | None = None,
    deleted_at: datetime | None = None,
) -> DiscussionStrategyTemplate:
    return DiscussionStrategyTemplate(
        owner_id=owner_id,
        name="auto", topic="t", rules="r", market="TW",
        persona_ids=["bull", "bear"],
        default_rounds=1, default_concurrency=1,
        default_auto_post_mortem=True,
        auto_schedule_enabled=enabled,
        auto_schedule_cadence_hours=cadence_hours,
        auto_schedule_anchor_offset_days=-1,
        auto_schedule_trading_days_count=1,
        auto_schedule_last_run_at=last_run_at,
        deleted_at=deleted_at,
    )


# ── _resolve_anchor (DB-backed: snaps to latest ohlcv_daily) ────


@pytest.mark.asyncio
async def test_resolve_anchor_snaps_to_latest_ohlcv_bar(
    db_session: AsyncSession,
):
    from models.ohlcv_daily import OhlcvDaily
    db_session.add_all([
        OhlcvDaily(
            market="TW", symbol="2330", ts=date(2026, 5, 28),
            open=1000, high=1010, low=990, close=1005, volume=1000,
            source="test",
        ),
        OhlcvDaily(
            market="TW", symbol="2330", ts=date(2026, 5, 29),
            open=1005, high=1015, low=1000, close=1010, volume=1000,
            source="test",
        ),
    ])
    await db_session.commit()

    # offset=0 + today's bar present → uses today
    assert await svc._resolve_anchor(
        db_session, base_today=date(2026, 5, 29), offset_days=0,
        market="TW",
    ) == date(2026, 5, 29)
    # offset=-1 → snaps to bar at-or-before yesterday
    assert await svc._resolve_anchor(
        db_session, base_today=date(2026, 5, 29), offset_days=-1,
        market="TW",
    ) == date(2026, 5, 28)


@pytest.mark.asyncio
async def test_resolve_anchor_snaps_back_when_today_not_ingested(
    db_session: AsyncSession,
):
    """offset=0 candidate is today, but only yesterday's bar is in
    the archive yet (EOD ingest hasn't completed) → snap back to
    yesterday rather than letting the sweep walk-forward to empty."""
    from models.ohlcv_daily import OhlcvDaily
    db_session.add(OhlcvDaily(
        market="TW", symbol="2330", ts=date(2026, 5, 28),
        open=1000, high=1010, low=990, close=1005, volume=1000,
        source="test",
    ))
    await db_session.commit()
    assert await svc._resolve_anchor(
        db_session, base_today=date(2026, 5, 29), offset_days=0,
        market="TW",
    ) == date(2026, 5, 28)


@pytest.mark.asyncio
async def test_resolve_anchor_falls_through_when_archive_empty(
    db_session: AsyncSession,
):
    """No rows in `ohlcv_daily` for the market → returns the raw
    candidate. Caller's `_resolve_trading_days` then bails cleanly."""
    assert await svc._resolve_anchor(
        db_session, base_today=date(2026, 5, 29), offset_days=-1,
        market="TW",
    ) == date(2026, 5, 28)
    assert await svc._resolve_anchor(
        db_session, base_today=date(2026, 5, 29), offset_days=0,
        market="US",
    ) == date(2026, 5, 29)


@pytest.mark.asyncio
async def test_resolve_anchor_ignores_synthetic_index_rows(
    db_session: AsyncSession,
):
    """`_TAIEX` and other underscore-prefixed synthetic bars are
    skipped — they're updated by the TAIEX history cron and would
    otherwise mask a stale per-stock archive."""
    from models.ohlcv_daily import OhlcvDaily
    db_session.add(OhlcvDaily(
        market="TW", symbol="_TAIEX", ts=date(2026, 5, 29),
        open=18000, high=18100, low=17900, close=18050, volume=0,
        source="test",
    ))
    await db_session.commit()
    assert await svc._resolve_anchor(
        db_session, base_today=date(2026, 5, 29), offset_days=0,
        market="TW",
    ) == date(2026, 5, 29)  # falls through to raw candidate


# ── _fetch_due_templates ───────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_due_excludes_disabled(
    db_session: AsyncSession, owner: User,
):
    t1 = _make_template(owner.id, enabled=False)
    t2 = _make_template(owner.id, enabled=True)
    db_session.add_all([t1, t2])
    await db_session.commit()
    rows = await svc._fetch_due_templates(db_session)
    assert {r.id for r in rows} == {t2.id}


@pytest.mark.asyncio
async def test_fetch_due_excludes_soft_deleted(
    db_session: AsyncSession, owner: User,
):
    t = _make_template(
        owner.id, enabled=True, deleted_at=datetime.now(UTC),
    )
    db_session.add(t)
    await db_session.commit()
    assert await svc._fetch_due_templates(db_session) == []


@pytest.mark.asyncio
async def test_fetch_due_respects_cadence(
    db_session: AsyncSession, owner: User,
):
    now = datetime.now(UTC)
    fresh = _make_template(
        owner.id, cadence_hours=24,
        last_run_at=now - timedelta(hours=1),  # too fresh
    )
    stale = _make_template(
        owner.id, cadence_hours=24,
        last_run_at=now - timedelta(hours=25),  # past cadence
    )
    never = _make_template(owner.id, last_run_at=None)
    db_session.add_all([fresh, stale, never])
    await db_session.commit()
    rows = await svc._fetch_due_templates(db_session)
    assert {r.id for r in rows} == {stale.id, never.id}


# ── process_due_strategies (DB-backed, sweep mocked) ──────────


@pytest.mark.asyncio
async def test_process_creates_and_starts_sweep(
    db_session: AsyncSession, owner: User,
):
    tmpl = _make_template(owner.id)
    db_session.add(tmpl)
    await db_session.commit()
    await db_session.refresh(tmpl)

    with patch(
        "services.strategy_auto_sweep_service.AsyncSessionLocal",
        return_value=_session_ctx(db_session),
    ):
        with patch(
            "services.strategy_auto_sweep_service.sweep_svc."
            "start_sweep_in_background",
            new=MagicMock(),
        ) as start_mock:
            launched = await svc.process_due_strategies()

    assert len(launched) == 1
    # Sweep row was persisted with strategy_id back-pointing to the
    # template — PR-B's aggregator depends on this link.
    sweep_id = launched[0]
    sweep = await db_session.get(BacktestSweep, sweep_id)
    assert sweep is not None
    assert sweep.strategy_id == tmpl.id
    assert sweep.market == tmpl.market
    assert sweep.persona_ids == tmpl.persona_ids
    start_mock.assert_called_once_with(sweep_id)

    # last_run_at bumped so the next tick won't immediately re-fire.
    await db_session.refresh(tmpl)
    assert tmpl.auto_schedule_last_run_at is not None


@pytest.mark.asyncio
async def test_process_skips_when_active_sweep_exists(
    db_session: AsyncSession, owner: User,
):
    tmpl = _make_template(owner.id)
    db_session.add(tmpl)
    await db_session.commit()
    await db_session.refresh(tmpl)

    db_session.add(BacktestSweep(
        owner_id=owner.id, strategy_id=tmpl.id,
        topic=tmpl.topic, rules=tmpl.rules, market=tmpl.market,
        persona_ids=list(tmpl.persona_ids),
        anchor_date=date(2026, 5, 5),
        trading_days_count=1, rounds_per_discussion=1,
        concurrency=1, auto_post_mortem=True,
        status="running",
    ))
    await db_session.commit()

    with patch(
        "services.strategy_auto_sweep_service.AsyncSessionLocal",
        return_value=_session_ctx(db_session),
    ):
        with patch(
            "services.strategy_auto_sweep_service.sweep_svc."
            "start_sweep_in_background",
            new=MagicMock(),
        ) as start_mock:
            launched = await svc.process_due_strategies()

    assert launched == []
    # No new background start because an active sweep already exists.
    start_mock.assert_not_called()
    # last_run_at still bumped so we don't re-check every tick.
    await db_session.refresh(tmpl)
    assert tmpl.auto_schedule_last_run_at is not None


def _session_ctx(session: AsyncSession):
    """Return a context-manager-like object that yields the test
    session so `async with AsyncSessionLocal() as db` reuses our
    in-memory test transaction instead of opening a real one."""
    class _Ctx:
        async def __aenter__(self_inner):
            return session
        async def __aexit__(self_inner, exc_type, exc, tb):
            return False
    return _Ctx()


# ── PR-A2: production sweep picks up walk-forward validated weights ──


@pytest.mark.asyncio
async def test_process_uses_validated_weights_when_available(
    db_session: AsyncSession, owner: User,
):
    """When a strategy has a recently completed walk-forward test
    sweep, the auto-schedule's production sweep should be created
    with that fold's weights as `weights_override`."""
    tmpl = _make_template(owner.id)
    db_session.add(tmpl)
    await db_session.commit()
    await db_session.refresh(tmpl)

    # Seed a completed test sweep carrying frozen weights.
    validated_weights = {"bull": 1.7, "bear": 0.6}
    db_session.add(BacktestSweep(
        owner_id=owner.id, strategy_id=tmpl.id,
        topic=tmpl.topic, rules=tmpl.rules, market=tmpl.market,
        persona_ids=list(tmpl.persona_ids),
        anchor_date=date(2026, 5, 1),
        trading_days_count=20, rounds_per_discussion=1,
        concurrency=1, auto_post_mortem=False,
        fold_kind="test",
        weights_override=validated_weights,
        status="completed",
        completed_at=datetime.now(UTC) - timedelta(days=1),
    ))
    await db_session.commit()

    with patch(
        "services.strategy_auto_sweep_service.AsyncSessionLocal",
        return_value=_session_ctx(db_session),
    ):
        with patch(
            "services.strategy_auto_sweep_service.sweep_svc."
            "start_sweep_in_background",
            new=MagicMock(),
        ):
            launched = await svc.process_due_strategies()

    assert len(launched) == 1
    sweep = await db_session.get(BacktestSweep, launched[0])
    assert sweep is not None
    assert sweep.fold_kind == "production"
    assert sweep.weights_override == validated_weights


@pytest.mark.asyncio
async def test_process_falls_back_to_no_override_when_no_validation(
    db_session: AsyncSession, owner: User,
):
    """Strategies without any walk-forward history get the legacy
    behavior: production sweep created without weights_override,
    Phase 3 will run the in-sample retrain."""
    tmpl = _make_template(owner.id)
    db_session.add(tmpl)
    await db_session.commit()
    await db_session.refresh(tmpl)

    with patch(
        "services.strategy_auto_sweep_service.AsyncSessionLocal",
        return_value=_session_ctx(db_session),
    ):
        with patch(
            "services.strategy_auto_sweep_service.sweep_svc."
            "start_sweep_in_background",
            new=MagicMock(),
        ):
            launched = await svc.process_due_strategies()

    assert len(launched) == 1
    sweep = await db_session.get(BacktestSweep, launched[0])
    assert sweep is not None
    assert sweep.fold_kind == "production"
    assert sweep.weights_override is None
