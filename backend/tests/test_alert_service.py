"""
Pure unit tests for AlertService — CRUD and check_and_fire logic.

All DB work uses the in-process SQLite test engine (db_session fixture).
notify_user (the transport-agnostic dispatch in services.notification_service,
re-exported through services.alert_service) is mocked so no WebSocket
infrastructure is needed.
"""
import uuid
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User, UserRole
from models.alert import AlertCondition, AlertEvent
from api.alerts.schemas import AlertCreate
from services.alert_service import AlertService


# ── helpers ──────────────────────────────────────────────────────────

async def _make_user(db: AsyncSession) -> User:
    u = User(
        email=f"alert_{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.viewer,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


def _alert_body(
    symbol: str = "AAPL",
    market: str = "US",
    condition: str = "above",
    target_price: float = 200.0,
) -> AlertCreate:
    return AlertCreate(symbol=symbol, market=market, condition=condition, target_price=target_price)


# ── list ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_empty_for_new_user(db_session: AsyncSession):
    user = await _make_user(db_session)
    result = await AlertService.list(db_session, user.id)
    assert result == []


@pytest.mark.asyncio
async def test_list_returns_only_own_alerts(db_session: AsyncSession):
    u1 = await _make_user(db_session)
    u2 = await _make_user(db_session)
    await AlertService.create(db_session, u1.id, _alert_body("AAPL"))
    result = await AlertService.list(db_session, u2.id)
    assert result == []


# ── create ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_alert_persisted(db_session: AsyncSession):
    user = await _make_user(db_session)
    body = _alert_body("MSFT", "US", "below", 300.0)
    alert = await AlertService.create(db_session, user.id, body)

    assert alert.symbol == "MSFT"
    assert alert.condition == AlertCondition.below
    assert alert.target_price == 300.0
    assert alert.triggered is False
    assert alert.triggered_at is None


@pytest.mark.asyncio
async def test_create_alert_symbol_uppercased(db_session: AsyncSession):
    user = await _make_user(db_session)
    alert = await AlertService.create(db_session, user.id, _alert_body("tsla"))
    assert alert.symbol == "TSLA"


@pytest.mark.asyncio
async def test_list_after_create(db_session: AsyncSession):
    user = await _make_user(db_session)
    await AlertService.create(db_session, user.id, _alert_body("NVDA"))
    await AlertService.create(db_session, user.id, _alert_body("AMD"))
    alerts = await AlertService.list(db_session, user.id)
    symbols = {a.symbol for a in alerts}
    assert "NVDA" in symbols
    assert "AMD" in symbols


# ── delete ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_own_alert(db_session: AsyncSession):
    user = await _make_user(db_session)
    alert = await AlertService.create(db_session, user.id, _alert_body("GOOG"))
    ok = await AlertService.delete(db_session, user.id, alert.id)
    assert ok is True
    remaining = await AlertService.list(db_session, user.id)
    assert all(a.id != alert.id for a in remaining)


@pytest.mark.asyncio
async def test_delete_other_user_alert_returns_false(db_session: AsyncSession):
    u1 = await _make_user(db_session)
    u2 = await _make_user(db_session)
    alert = await AlertService.create(db_session, u1.id, _alert_body("META"))
    ok = await AlertService.delete(db_session, u2.id, alert.id)
    assert ok is False


@pytest.mark.asyncio
async def test_delete_nonexistent_returns_false(db_session: AsyncSession):
    user = await _make_user(db_session)
    ok = await AlertService.delete(db_session, user.id, uuid.uuid4())
    assert ok is False


# ── check_and_fire ────────────────────────────────────────────────────
# Each test uses a unique symbol (CFx prefix) to prevent cross-test
# contamination in the session-scoped in-memory database.

@pytest.mark.asyncio
async def test_check_and_fire_above_triggers(db_session: AsyncSession):
    """Alert 'price above 200' fires when current_price = 205."""
    user = await _make_user(db_session)
    alert = await AlertService.create(db_session, user.id, _alert_body("CF1", "US", "above", 200.0))

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(db_session, "CF1", "US", 205.0)

    await db_session.refresh(alert)
    assert alert.triggered is True
    assert alert.triggered_at is not None
    mock_push.assert_awaited_once()
    payload = mock_push.call_args[0][1]
    assert payload["symbol"] == "CF1"
    assert payload["current_price"] == 205.0
    assert payload["condition"] == "above"


@pytest.mark.asyncio
async def test_check_and_fire_below_triggers(db_session: AsyncSession):
    """Alert 'price below 150' fires when current_price = 148."""
    user = await _make_user(db_session)
    alert = await AlertService.create(db_session, user.id, _alert_body("CF2", "US", "below", 150.0))

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(db_session, "CF2", "US", 148.0)

    await db_session.refresh(alert)
    assert alert.triggered is True
    mock_push.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_and_fire_above_not_triggered_when_price_below(db_session: AsyncSession):
    """Alert 'price above 200' must NOT fire when current_price = 195."""
    user = await _make_user(db_session)
    alert = await AlertService.create(db_session, user.id, _alert_body("CF3", "US", "above", 200.0))

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(db_session, "CF3", "US", 195.0)

    await db_session.refresh(alert)
    assert alert.triggered is False
    mock_push.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_and_fire_below_not_triggered_when_price_above(db_session: AsyncSession):
    """Alert 'price below 150' must NOT fire when current_price = 155."""
    user = await _make_user(db_session)
    alert = await AlertService.create(db_session, user.id, _alert_body("CF4", "US", "below", 150.0))

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(db_session, "CF4", "US", 155.0)

    await db_session.refresh(alert)
    assert alert.triggered is False
    mock_push.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_and_fire_exact_boundary_above_triggers(db_session: AsyncSession):
    """Condition 'above' fires when price equals the target exactly (>=)."""
    user = await _make_user(db_session)
    alert = await AlertService.create(db_session, user.id, _alert_body("CF5", "US", "above", 200.0))

    with patch("services.alert_service.notify_user", new_callable=AsyncMock):
        await AlertService.check_and_fire(db_session, "CF5", "US", 200.0)

    await db_session.refresh(alert)
    assert alert.triggered is True


@pytest.mark.asyncio
async def test_check_and_fire_already_triggered_not_fired_again(db_session: AsyncSession):
    """An already-triggered alert is never pushed again."""
    user = await _make_user(db_session)
    await AlertService.create(db_session, user.id, _alert_body("CF6", "US", "above", 100.0))

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        # First firing
        await AlertService.check_and_fire(db_session, "CF6", "US", 950.0)
        assert mock_push.await_count == 1
        # Second call — alert already triggered, query filters it out
        await AlertService.check_and_fire(db_session, "CF6", "US", 960.0)
        assert mock_push.await_count == 1  # still 1


@pytest.mark.asyncio
async def test_check_and_fire_multiple_alerts_same_symbol(db_session: AsyncSession):
    """Two different users can have alerts for the same symbol; both fire."""
    u1 = await _make_user(db_session)
    u2 = await _make_user(db_session)
    a1 = await AlertService.create(db_session, u1.id, _alert_body("CF7", "US", "above", 100.0))
    a2 = await AlertService.create(db_session, u2.id, _alert_body("CF7", "US", "above", 110.0))

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(db_session, "CF7", "US", 115.0)

    await db_session.refresh(a1)
    await db_session.refresh(a2)
    assert a1.triggered is True
    assert a2.triggered is True
    assert mock_push.await_count == 2


@pytest.mark.asyncio
async def test_check_and_fire_no_alerts_returns_without_push(db_session: AsyncSession):
    """When no untriggered alerts match the symbol, push must not be called."""
    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(db_session, "CF_NONEXISTENT", "US", 999.0)

    mock_push.assert_not_awaited()


# ── alert_events history (PR-D5) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_check_and_fire_writes_alert_event_row(db_session: AsyncSession):
    """On fire, an alert_events history row lands in the same
    transaction as the triggered-flag flip."""
    user = await _make_user(db_session)
    alert = await AlertService.create(db_session, user.id, _alert_body("CF9", "US", "above", 200.0))

    with patch("services.alert_service.notify_user", new_callable=AsyncMock):
        await AlertService.check_and_fire(db_session, "CF9", "US", 210.0)

    events = list((await db_session.scalars(
        select(AlertEvent).where(AlertEvent.user_id == user.id)
    )).all())
    assert len(events) == 1
    ev = events[0]
    assert ev.alert_id == alert.id
    assert ev.symbol == "CF9"
    assert ev.market == "US"
    assert ev.kind == "price"
    assert "CF9" in ev.message
    assert ev.fired_at is not None
    assert ev.payload["condition"] == "above"
    assert ev.payload["target_price"] == 200.0
    assert ev.payload["current_price"] == 210.0


@pytest.mark.asyncio
async def test_check_and_fire_no_event_row_when_not_triggered(db_session: AsyncSession):
    user = await _make_user(db_session)
    await AlertService.create(db_session, user.id, _alert_body("CF10", "US", "above", 200.0))

    with patch("services.alert_service.notify_user", new_callable=AsyncMock):
        await AlertService.check_and_fire(db_session, "CF10", "US", 195.0)

    events = list((await db_session.scalars(
        select(AlertEvent).where(AlertEvent.user_id == user.id)
    )).all())
    assert events == []


@pytest.mark.asyncio
async def test_history_newest_first_with_cursor(db_session: AsyncSession):
    """`before` cursor pages strictly-older rows, newest first."""
    user = await _make_user(db_session)
    base = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    for i in range(5):
        db_session.add(AlertEvent(
            user_id=user.id, symbol=f"H{i}", market="US",
            kind="price", message=f"m{i}",
            fired_at=base + timedelta(minutes=i),
        ))
    await db_session.commit()

    page1 = await AlertService.history(db_session, user.id, limit=2)
    assert [e.symbol for e in page1] == ["H4", "H3"]

    page2 = await AlertService.history(
        db_session, user.id, limit=2, before=page1[-1].fired_at,
    )
    assert [e.symbol for e in page2] == ["H2", "H1"]


@pytest.mark.asyncio
async def test_history_scoped_to_user(db_session: AsyncSession):
    """No cross-user leak: each user only sees their own events."""
    u1 = await _make_user(db_session)
    u2 = await _make_user(db_session)
    db_session.add(AlertEvent(
        user_id=u1.id, symbol="MINE", market="US", kind="price", message="x",
        fired_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    assert [e.symbol for e in await AlertService.history(db_session, u1.id)] == ["MINE"]
    assert await AlertService.history(db_session, u2.id) == []


@pytest.mark.asyncio
async def test_check_and_fire_wrong_market_not_matched(db_session: AsyncSession):
    """Symbol matches but market differs — alert must not fire."""
    user = await _make_user(db_session)
    alert = await AlertService.create(
        db_session, user.id, _alert_body("CF8", "TW", "above", 500.0)
    )

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(db_session, "CF8", "US", 600.0)

    await db_session.refresh(alert)
    assert alert.triggered is False
    mock_push.assert_not_awaited()
