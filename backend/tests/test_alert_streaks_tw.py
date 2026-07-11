"""外資連 N 日買超 daily alert task (PR-D1, tasks/alert_streaks_tw.py).

Seeds tw_institutional_daily rows and drives
`check_foreign_streak_alerts` directly with the test session;
notify_user is mocked like the tick-path tests.
"""
import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.alert import AlertEvent, PriceAlert
from models.tw_chip_metrics import TwInstitutionalDaily
from models.user import User, UserRole
from tasks.alert_streaks_tw import check_foreign_streak_alerts


async def _make_user(db: AsyncSession) -> User:
    u = User(
        email=f"streak_{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.viewer,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _seed_institutional(
    db: AsyncSession, symbol: str, nets: list[int],
) -> None:
    """One row per net value, newest last (ending yesterday).
    net > 0 → fini_buy > fini_sell."""
    today = date.today()
    for i, net in enumerate(nets):
        base = 1_000_000
        db.add(TwInstitutionalDaily(
            market="TW", symbol=symbol,
            ts=today - timedelta(days=len(nets) - i),
            fini_buy=base + max(net, 0),
            fini_sell=base + max(-net, 0),
            sitc_buy=0, sitc_sell=0, dealer_buy=0, dealer_sell=0,
            source="test",
        ))
    await db.commit()


def _streak_alert(user_id, symbol: str, days: int, **kw) -> PriceAlert:
    return PriceAlert(
        user_id=user_id, symbol=symbol, market="TW",
        condition_type="foreign_net_buy_streak",
        params={"days": days}, **kw,
    )


@pytest.mark.asyncio
async def test_streak_fires_when_all_days_net_buy(db_session: AsyncSession):
    user = await _make_user(db_session)
    await _seed_institutional(db_session, "2330", [500, 1200, 300])
    alert = _streak_alert(user.id, "2330", days=3)
    db_session.add(alert)
    await db_session.commit()

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        fired = await check_foreign_streak_alerts(db_session)

    assert fired == 1
    await db_session.refresh(alert)
    assert alert.triggered is True
    assert alert.last_fired_at is not None
    mock_push.assert_awaited_once()
    payload = mock_push.call_args[0][1]
    assert payload["condition_type"] == "foreign_net_buy_streak"
    assert payload["days"] == 3

    events = list((await db_session.scalars(
        select(AlertEvent).where(AlertEvent.alert_id == alert.id)
    )).all())
    assert len(events) == 1
    assert "外資連 3 日買超" in events[0].message


@pytest.mark.asyncio
async def test_streak_not_fired_on_one_net_sell_day(db_session: AsyncSession):
    user = await _make_user(db_session)
    await _seed_institutional(db_session, "2317", [500, -100, 300])
    alert = _streak_alert(user.id, "2317", days=3)
    db_session.add(alert)
    await db_session.commit()

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        fired = await check_foreign_streak_alerts(db_session)

    assert fired == 0
    await db_session.refresh(alert)
    assert alert.triggered is False
    mock_push.assert_not_awaited()


@pytest.mark.asyncio
async def test_streak_uses_most_recent_window(db_session: AsyncSession):
    """A sell day OLDER than the window must not block the streak."""
    user = await _make_user(db_session)
    await _seed_institutional(db_session, "2454", [-999, 500, 1200, 300])
    db_session.add(_streak_alert(user.id, "2454", days=3))
    await db_session.commit()

    with patch("services.alert_service.notify_user", new_callable=AsyncMock):
        assert await check_foreign_streak_alerts(db_session) == 1


@pytest.mark.asyncio
async def test_streak_insufficient_history_abstains(db_session: AsyncSession):
    user = await _make_user(db_session)
    await _seed_institutional(db_session, "2603", [500, 300])  # only 2 rows
    db_session.add(_streak_alert(user.id, "2603", days=3))
    await db_session.commit()

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        assert await check_foreign_streak_alerts(db_session) == 0
    mock_push.assert_not_awaited()


@pytest.mark.asyncio
async def test_streak_zero_net_day_is_not_a_buy_day(db_session: AsyncSession):
    """Net == 0 breaks the streak (strictly positive required)."""
    user = await _make_user(db_session)
    await _seed_institutional(db_session, "2881", [500, 0, 300])
    db_session.add(_streak_alert(user.id, "2881", days=3))
    await db_session.commit()

    with patch("services.alert_service.notify_user", new_callable=AsyncMock):
        assert await check_foreign_streak_alerts(db_session) == 0


@pytest.mark.asyncio
async def test_streak_repeat_respects_cooldown(db_session: AsyncSession):
    """A repeat streak alert re-fires only after its cooldown; the
    daily task calling twice in a row (e.g. manual re-run) must not
    double-fire inside the window."""
    user = await _make_user(db_session)
    await _seed_institutional(db_session, "2882", [500, 1200, 300])
    alert = _streak_alert(
        user.id, "2882", days=3, repeat=True, cooldown_seconds=3600,
    )
    db_session.add(alert)
    await db_session.commit()

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        assert await check_foreign_streak_alerts(db_session) == 1
        # triggered flag untouched, but cooldown blocks the re-run
        assert await check_foreign_streak_alerts(db_session) == 0
    assert mock_push.await_count == 1
    await db_session.refresh(alert)
    assert alert.triggered is False


@pytest.mark.asyncio
async def test_streak_ignores_non_streak_alerts(db_session: AsyncSession):
    """Tick-evaluated alert types are not the daily task's business."""
    user = await _make_user(db_session)
    db_session.add(PriceAlert(
        user_id=user.id, symbol="2891", market="TW",
        condition_type="price_above", target_price=10.0,
    ))
    await db_session.commit()

    with patch("services.alert_service.notify_user", new_callable=AsyncMock) as mock_push:
        assert await check_foreign_streak_alerts(db_session) == 0
    mock_push.assert_not_awaited()
