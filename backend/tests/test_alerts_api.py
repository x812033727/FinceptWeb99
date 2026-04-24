"""Integration tests for price alert CRUD and check-and-fire logic."""
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.alert import AlertCondition, PriceAlert
from services.alert_service import AlertService


async def _auth_headers(client: AsyncClient, email: str = "alerts@example.com") -> dict[str, str]:
    await client.post("/api/auth/register", json={
        "email": email,
        "password": "ValidPass99!",
    })
    r = await client.post("/api/auth/login", json={
        "email": email,
        "password": "ValidPass99!",
    })
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_list_alerts_empty(client: AsyncClient):
    h = await _auth_headers(client, "list_empty@example.com")
    r = await client.get("/api/alerts", headers=h)
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_create_alert(client: AsyncClient):
    h = await _auth_headers(client, "create_alert@example.com")
    r = await client.post("/api/alerts", json={
        "symbol": "AAPL",
        "market": "US",
        "condition": "above",
        "target_price": 200.0,
    }, headers=h)
    assert r.status_code == 201
    data = r.json()
    assert data["symbol"] == "AAPL"
    assert data["condition"] == "above"
    assert data["target_price"] == 200.0
    assert data["triggered"] is False


@pytest.mark.asyncio
async def test_create_alert_invalid_market(client: AsyncClient):
    h = await _auth_headers(client, "bad_market@example.com")
    r = await client.post("/api/alerts", json={
        "symbol": "AAPL",
        "market": "JP",
        "condition": "above",
        "target_price": 100.0,
    }, headers=h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_delete_alert(client: AsyncClient):
    h = await _auth_headers(client, "del_alert@example.com")
    create_r = await client.post("/api/alerts", json={
        "symbol": "TSLA",
        "market": "US",
        "condition": "below",
        "target_price": 150.0,
    }, headers=h)
    alert_id = create_r.json()["id"]

    r = await client.delete(f"/api/alerts/{alert_id}", headers=h)
    assert r.status_code == 204

    alerts = (await client.get("/api/alerts", headers=h)).json()
    assert all(a["id"] != alert_id for a in alerts)


@pytest.mark.asyncio
async def test_delete_nonexistent_alert(client: AsyncClient):
    h = await _auth_headers(client, "del_none@example.com")
    import uuid
    r = await client.delete(f"/api/alerts/{uuid.uuid4()}", headers=h)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_alert_requires_auth(client: AsyncClient):
    r = await client.get("/api/alerts")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_check_and_fire_above(db_session: AsyncSession):
    """check_and_fire marks alert triggered and calls push (mocked via monkeypatch)."""
    import uuid
    from unittest.mock import AsyncMock, patch

    user_id = uuid.uuid4()
    alert = PriceAlert(
        user_id=user_id,
        symbol="NVDA",
        market="US",
        condition=AlertCondition.above,
        target_price=500.0,
        triggered=False,
    )
    db_session.add(alert)
    await db_session.commit()

    with patch("api.websocket.manager.push_alert_to_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(db_session, "NVDA", "US", current_price=520.0)
        mock_push.assert_awaited_once()

    await db_session.refresh(alert)
    assert alert.triggered is True
    assert alert.triggered_at is not None


@pytest.mark.asyncio
async def test_check_and_fire_below_not_triggered(db_session: AsyncSession):
    """Alert with condition=below is NOT fired if price is still above target."""
    import uuid
    from unittest.mock import AsyncMock, patch

    user_id = uuid.uuid4()
    alert = PriceAlert(
        user_id=user_id,
        symbol="META",
        market="US",
        condition=AlertCondition.below,
        target_price=300.0,
        triggered=False,
    )
    db_session.add(alert)
    await db_session.commit()

    with patch("api.websocket.manager.push_alert_to_user", new_callable=AsyncMock) as mock_push:
        await AlertService.check_and_fire(db_session, "META", "US", current_price=350.0)
        mock_push.assert_not_awaited()

    await db_session.refresh(alert)
    assert alert.triggered is False
