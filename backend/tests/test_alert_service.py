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


# ── rule engine (PR-D1) ──────────────────────────────────────────
# check_and_fire with the new condition types, repeat/cooldown
# semantics, and daily-threshold resolution from seeded ohlcv_daily.
# Redis is mocked to always miss (conftest), so threshold resolution
# exercises the DB fallback path every time.

from datetime import date  # noqa: E402

from models.alert import PriceAlert  # noqa: E402
from models.ohlcv_daily import OhlcvDaily  # noqa: E402
from schemas.alert import AlertUpdate  # noqa: E402
from services.alert_service import cooldown_ok  # noqa: E402


def _rule_body(
    symbol: str,
    condition_type: str,
    *,
    market: str = "US",
    params: dict | None = None,
    target_price: float | None = None,
    repeat: bool = False,
    cooldown_seconds: int = 0,
) -> AlertCreate:
    return AlertCreate(
        symbol=symbol, market=market,
        condition_type=condition_type, params=params,
        target_price=target_price,
        repeat=repeat, cooldown_seconds=cooldown_seconds,
    )


async def _seed_ohlcv(
    db: AsyncSession, symbol: str, bars: list[tuple[float, float, int]],
    market: str = "US",
) -> None:
    """Seed (high, low, volume) daily bars ending yesterday."""
    today = date.today()
    for i, (high, low, vol) in enumerate(bars):
        db.add(OhlcvDaily(
            market=market, symbol=symbol,
            ts=today - timedelta(days=len(bars) - i),
            open=low, high=high, low=low, close=high,
            volume=vol, source="test",
        ))
    await db.commit()


@pytest.mark.asyncio
async def test_pct_change_above_fires_from_quote(db_session: AsyncSession):
    user = await _make_user(db_session)
    alert = await AlertService.create(
        db_session, user.id,
        _rule_body("RE1", "pct_change_above", params={"pct": 5.0}),
    )

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        # below threshold — no fire
        await AlertService.check_and_fire(
            db_session, "RE1", "US", 104.9, quote={"change_pct": 4.9},
        )
        mock_push.assert_not_awaited()
        # at/above threshold — fires
        await AlertService.check_and_fire(
            db_session, "RE1", "US", 106.2, quote={"change_pct": 6.2},
        )
        mock_push.assert_awaited_once()

    await db_session.refresh(alert)
    assert alert.triggered is True
    assert alert.last_fired_at is not None
    payload = mock_push.call_args[0][1]
    assert payload["condition_type"] == "pct_change_above"
    assert payload["change_pct"] == 6.2


@pytest.mark.asyncio
async def test_pct_change_without_quote_payload_abstains(db_session: AsyncSession):
    """Legacy price-only call signature: quote-dependent rules must
    not fire (and must not crash)."""
    user = await _make_user(db_session)
    await AlertService.create(
        db_session, user.id,
        _rule_body("RE2", "pct_change_above", params={"pct": 1.0}),
    )
    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(db_session, "RE2", "US", 999.0)
    mock_push.assert_not_awaited()


@pytest.mark.asyncio
async def test_breakout_high_fires_above_ndays_high(db_session: AsyncSession):
    user = await _make_user(db_session)
    await _seed_ohlcv(db_session, "RE3", [(100, 90, 1000), (105, 95, 1000), (103, 93, 1000)])
    alert = await AlertService.create(
        db_session, user.id,
        _rule_body("RE3", "breakout_high", params={"lookback_days": 3}),
    )

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        # touching the 3-day high (105) is not a breakout
        await AlertService.check_and_fire(db_session, "RE3", "US", 105.0)
        mock_push.assert_not_awaited()
        await AlertService.check_and_fire(db_session, "RE3", "US", 105.5)
        mock_push.assert_awaited_once()

    await db_session.refresh(alert)
    assert alert.triggered is True
    payload = mock_push.call_args[0][1]
    assert payload["threshold"] == 105.0
    assert payload["lookback_days"] == 3


@pytest.mark.asyncio
async def test_breakout_low_fires_below_ndays_low(db_session: AsyncSession):
    user = await _make_user(db_session)
    await _seed_ohlcv(db_session, "RE4", [(100, 90, 1000), (105, 88, 1000), (103, 93, 1000)])
    await AlertService.create(
        db_session, user.id,
        _rule_body("RE4", "breakout_low", params={"lookback_days": 3}),
    )

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(db_session, "RE4", "US", 88.0)
        mock_push.assert_not_awaited()
        await AlertService.check_and_fire(db_session, "RE4", "US", 87.9)
        mock_push.assert_awaited_once()
    assert mock_push.call_args[0][1]["threshold"] == 88.0


@pytest.mark.asyncio
async def test_breakout_without_daily_bars_never_fires(db_session: AsyncSession):
    user = await _make_user(db_session)
    await AlertService.create(
        db_session, user.id,
        _rule_body("RE5", "breakout_high", params={"lookback_days": 20}),
    )
    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(db_session, "RE5", "US", 99999.0)
    mock_push.assert_not_awaited()


@pytest.mark.asyncio
async def test_volume_surge_fires_on_multiple_of_avg(db_session: AsyncSession):
    user = await _make_user(db_session)
    # avg volume = 1000
    await _seed_ohlcv(db_session, "RE6", [(100, 90, 800), (100, 90, 1200), (100, 90, 1000)])
    await AlertService.create(
        db_session, user.id,
        _rule_body("RE6", "volume_surge", params={"multiple": 2.0, "lookback_days": 3}),
    )

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(
            db_session, "RE6", "US", 100.0, quote={"volume": 1999},
        )
        mock_push.assert_not_awaited()
        await AlertService.check_and_fire(
            db_session, "RE6", "US", 100.0, quote={"volume": 2000},
        )
        mock_push.assert_awaited_once()
    payload = mock_push.call_args[0][1]
    assert payload["avg_volume"] == 1000.0
    assert payload["current_volume"] == 2000


@pytest.mark.asyncio
async def test_foreign_streak_skipped_on_tick(db_session: AsyncSession):
    """Daily condition types are never evaluated on the quote tick."""
    user = await _make_user(db_session)
    alert = await AlertService.create(
        db_session, user.id,
        _rule_body("2330", "foreign_net_buy_streak", market="TW", params={"days": 3}),
    )
    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(db_session, "2330", "TW", 600.0)
    await db_session.refresh(alert)
    assert alert.triggered is False
    mock_push.assert_not_awaited()


# ── repeat / cooldown semantics ──────────────────────────────────

@pytest.mark.asyncio
async def test_repeat_false_fires_once_and_disables(db_session: AsyncSession):
    """Default (repeat=False) keeps the original fire-once behavior."""
    user = await _make_user(db_session)
    alert = await AlertService.create(
        db_session, user.id,
        _rule_body("RE7", "price_above", target_price=100.0),
    )
    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(db_session, "RE7", "US", 101.0)
        await AlertService.check_and_fire(db_session, "RE7", "US", 102.0)
    assert mock_push.await_count == 1
    await db_session.refresh(alert)
    assert alert.triggered is True


@pytest.mark.asyncio
async def test_repeat_true_refires_after_cooldown(db_session: AsyncSession):
    user = await _make_user(db_session)
    alert = await AlertService.create(
        db_session, user.id,
        _rule_body(
            "RE8", "price_above", target_price=100.0,
            repeat=True, cooldown_seconds=600,
        ),
    )

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(db_session, "RE8", "US", 101.0)
        assert mock_push.await_count == 1
        await db_session.refresh(alert)
        assert alert.triggered is False        # repeat never disables
        assert alert.last_fired_at is not None
        first_fired_at = alert.last_fired_at

        # Inside the cooldown window — no re-fire.
        await AlertService.check_and_fire(db_session, "RE8", "US", 102.0)
        assert mock_push.await_count == 1

        # Manually age last_fired_at past the cooldown (ts injection).
        alert.last_fired_at = datetime.now(timezone.utc) - timedelta(seconds=601)
        await db_session.commit()

        await AlertService.check_and_fire(db_session, "RE8", "US", 103.0)
        assert mock_push.await_count == 2

    await db_session.refresh(alert)
    assert alert.triggered is False
    assert alert.last_fired_at != first_fired_at

    # Both firings left history rows.
    events = list((await db_session.scalars(
        select(AlertEvent).where(AlertEvent.alert_id == alert.id)
    )).all())
    assert len(events) == 2
    assert all(ev.payload["condition_type"] == "price_above" for ev in events)


@pytest.mark.asyncio
async def test_repeat_true_cooldown_zero_refires_every_tick(db_session: AsyncSession):
    user = await _make_user(db_session)
    await AlertService.create(
        db_session, user.id,
        _rule_body("RE9", "price_above", target_price=100.0,
                   repeat=True, cooldown_seconds=0),
    )
    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(db_session, "RE9", "US", 101.0)
        await AlertService.check_and_fire(db_session, "RE9", "US", 102.0)
    assert mock_push.await_count == 2


def test_cooldown_ok_pure_semantics():
    now = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    # once-only: gate is the triggered flag
    a = PriceAlert(repeat=False, triggered=False, cooldown_seconds=0)
    assert cooldown_ok(a, now) is True
    a.triggered = True
    assert cooldown_ok(a, now) is False
    # repeat: gate is last_fired_at + cooldown
    r = PriceAlert(repeat=True, triggered=False, cooldown_seconds=600)
    assert cooldown_ok(r, now) is True                       # never fired
    r.last_fired_at = now - timedelta(seconds=599)
    assert cooldown_ok(r, now) is False                      # inside window
    r.last_fired_at = now - timedelta(seconds=600)
    assert cooldown_ok(r, now) is True                       # boundary
    # naive timestamp (SQLite) treated as UTC
    r.last_fired_at = (now - timedelta(seconds=601)).replace(tzinfo=None)
    assert cooldown_ok(r, now) is True


# ── update (PR-D1 PATCH) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_rule_knobs(db_session: AsyncSession):
    user = await _make_user(db_session)
    alert = await AlertService.create(
        db_session, user.id,
        _rule_body("RE10", "pct_change_above", params={"pct": 5.0}),
    )
    updated = await AlertService.update(
        db_session, user.id, alert.id,
        AlertUpdate(params={"pct": 7.5}, repeat=True, cooldown_seconds=3600),
    )
    assert updated.params == {"pct": 7.5}
    assert updated.repeat is True
    assert updated.cooldown_seconds == 3600


@pytest.mark.asyncio
async def test_update_rejects_params_mismatching_condition_type(db_session: AsyncSession):
    user = await _make_user(db_session)
    alert = await AlertService.create(
        db_session, user.id,
        _rule_body("RE11", "pct_change_above", params={"pct": 5.0}),
    )
    with pytest.raises(ValueError):
        await AlertService.update(
            db_session, user.id, alert.id,
            AlertUpdate(params={"lookback_days": 20}),
        )


@pytest.mark.asyncio
async def test_update_other_user_returns_none(db_session: AsyncSession):
    u1 = await _make_user(db_session)
    u2 = await _make_user(db_session)
    alert = await AlertService.create(
        db_session, u1.id, _rule_body("RE12", "price_above", target_price=10.0),
    )
    assert await AlertService.update(
        db_session, u2.id, alert.id, AlertUpdate(repeat=True),
    ) is None


# ── legacy row compatibility ─────────────────────────────────────

@pytest.mark.asyncio
async def test_legacy_condition_row_still_fires(db_session: AsyncSession):
    """A pre-D1 style row (condition enum + condition_type mapped by
    the 0065 data migration) fires through the new engine."""
    user = await _make_user(db_session)
    legacy = PriceAlert(
        user_id=user.id, symbol="RE13", market="US",
        condition=AlertCondition.below, target_price=150.0,
        condition_type="price_below",   # what migration 0065 writes
    )
    db_session.add(legacy)
    await db_session.commit()

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(db_session, "RE13", "US", 148.0)

    await db_session.refresh(legacy)
    assert legacy.triggered is True
    payload = mock_push.call_args[0][1]
    assert payload["condition"] == "below"
    assert payload["condition_type"] == "price_below"
