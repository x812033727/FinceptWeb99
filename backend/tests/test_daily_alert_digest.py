"""PR-D5: tests for the daily alert digest email cron.

Mocks Redis lock + SMTP transport so the test runs purely against
the SQLite test DB — same conventions as test_monitor_strategy_health.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.alert import AlertEvent
from models.notification_channel import NotificationChannel
from models.user import User, UserRole
from tasks import daily_alert_digest as cron


async def _make_user(db: AsyncSession, email: str | None = None) -> User:
    u = User(
        email=email or f"digest_{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.viewer,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _add_event(
    db: AsyncSession, user_id: uuid.UUID, symbol: str,
    *, hours_ago: float = 1.0, kind: str = "price",
) -> None:
    db.add(AlertEvent(
        user_id=user_id, symbol=symbol, market="US", kind=kind,
        message=f"{symbol} fired",
        fired_at=datetime.now(UTC) - timedelta(hours=hours_ago),
    ))
    await db.commit()


async def _opt_in(db: AsyncSession, user_id: uuid.UUID) -> None:
    db.add(NotificationChannel(
        user_id=user_id, kind="email", enabled=False, verified=True,
        config={"event_kinds": ["price_alert", "strategy_health"], "daily_digest": True},
    ))
    await db.commit()


@pytest.mark.asyncio
async def test_skips_silently_when_smtp_unconfigured(db_session: AsyncSession):
    """Fail-closed gate: without SMTP config the cron neither takes
    the lock nor tries to send."""
    user = await _make_user(db_session)
    await _add_event(db_session, user.id, "SKIP1")

    send = AsyncMock()
    lock = AsyncMock(return_value=True)
    with patch.object(cron.email_service, "is_configured", return_value=False), \
         patch.object(cron.email_service, "send_email", send), \
         patch.object(cron, "acquire_lock", lock):
        out = await cron.run_daily_alert_digest()

    assert out["skipped"] == "smtp_not_configured"
    assert out["emails_sent"] == 0
    send.assert_not_awaited()
    lock.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_when_lock_held(db_session: AsyncSession):
    user = await _make_user(db_session)
    await _add_event(db_session, user.id, "LOCK1")

    send = AsyncMock()
    with patch.object(cron.email_service, "is_configured", return_value=True), \
         patch.object(cron.email_service, "send_email", send), \
         patch.object(cron, "acquire_lock", AsyncMock(return_value=False)), \
         patch.object(cron, "release_lock", AsyncMock()):
        out = await cron.run_daily_alert_digest()

    assert out["skipped"] == "lock_held"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_aggregates_per_user_last_24h(db_session: AsyncSession):
    """Two users with recent events each get exactly one email;
    events older than 24 h are excluded; per-user body lists only
    that user's symbols."""
    u1 = await _make_user(db_session)
    u2 = await _make_user(db_session)
    await _opt_in(db_session, u1.id)
    await _opt_in(db_session, u2.id)
    await _add_event(db_session, u1.id, "AAA", hours_ago=1)
    await _add_event(db_session, u1.id, "BBB", hours_ago=2, kind="strategy_health")
    await _add_event(db_session, u1.id, "OLD", hours_ago=30)   # outside window
    await _add_event(db_session, u2.id, "CCC", hours_ago=3)

    send = AsyncMock()
    with patch.object(cron.email_service, "is_configured", return_value=True), \
         patch.object(cron.email_service, "send_email", send), \
         patch.object(cron, "acquire_lock", AsyncMock(return_value=True)), \
         patch.object(cron, "release_lock", AsyncMock()):
        out = await cron.run_daily_alert_digest()

    assert out["users_with_events"] == 2
    assert out["emails_sent"] == 2
    assert out["errors"] == 0

    by_to = {c.kwargs["to"]: c.kwargs for c in send.await_args_list}
    body1 = by_to[u1.email]["body_markdown"]
    assert "AAA" in body1 and "BBB" in body1
    assert "OLD" not in body1
    assert "CCC" not in body1
    assert "2" in by_to[u1.email]["subject"]   # 2 筆
    body2 = by_to[u2.email]["body_markdown"]
    assert "CCC" in body2 and "AAA" not in body2


@pytest.mark.asyncio
async def test_one_bad_address_does_not_poison_loop(db_session: AsyncSession):
    u1 = await _make_user(db_session)
    u2 = await _make_user(db_session)
    await _opt_in(db_session, u1.id)
    await _opt_in(db_session, u2.id)
    await _add_event(db_session, u1.id, "ERR1")
    await _add_event(db_session, u2.id, "OK1")

    async def _flaky_send(*, to, **kw):
        if to == u1.email:
            raise RuntimeError("smtp boom")

    with patch.object(cron.email_service, "is_configured", return_value=True), \
         patch.object(cron.email_service, "send_email", AsyncMock(side_effect=_flaky_send)), \
         patch.object(cron, "acquire_lock", AsyncMock(return_value=True)), \
         patch.object(cron, "release_lock", AsyncMock()):
        out = await cron.run_daily_alert_digest()

    assert out["errors"] == 1
    assert out["emails_sent"] == 1


@pytest.mark.asyncio
async def test_no_events_sends_nothing(db_session: AsyncSession):
    await _make_user(db_session)
    send = AsyncMock()
    with patch.object(cron.email_service, "is_configured", return_value=True), \
         patch.object(cron.email_service, "send_email", send), \
         patch.object(cron, "acquire_lock", AsyncMock(return_value=True)), \
         patch.object(cron, "release_lock", AsyncMock()):
        out = await cron.run_daily_alert_digest()
    assert out["users_with_events"] == 0
    assert out["emails_sent"] == 0
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_digest_is_opt_in_not_an_unsolicited_email(db_session: AsyncSession):
    user = await _make_user(db_session)
    await _add_event(db_session, user.id, "NOOPT")
    send = AsyncMock()
    with patch.object(cron.email_service, "is_configured", return_value=True), \
         patch.object(cron.email_service, "send_email", send), \
         patch.object(cron, "acquire_lock", AsyncMock(return_value=True)), \
         patch.object(cron, "release_lock", AsyncMock()):
        out = await cron.run_daily_alert_digest()
    assert out["users_with_events"] == 0
    assert out["emails_sent"] == 0
    send.assert_not_awaited()
