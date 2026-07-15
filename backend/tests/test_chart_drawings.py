import uuid

import pytest
from httpx import AsyncClient
from models.alert import PriceAlert


async def _token(client: AsyncClient, email: str) -> str:
    await client.post("/api/auth/register", json={"email": email, "password": "Pass99!!"})
    response = await client.post("/api/auth/login", json={"email": email, "password": "Pass99!!"})
    return response.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_horizontal_drawing_crud_and_idempotent_alert_conversion(client: AsyncClient):
    token = await _token(client, "drawing@test.com")
    created = await client.post(
        "/api/charts/drawings", headers=_headers(token),
        json={
            "market": "TW", "symbol": "2330", "kind": "horizontal",
            "points": [{"price": 1000}], "label": "突破壓力", "color": "#f59e0b",
        },
    )
    assert created.status_code == 201, created.text
    drawing_id = created.json()["id"]
    assert created.json()["alert_id"] is None

    listing = await client.get("/api/charts/drawings/TW/2330", headers=_headers(token))
    assert [row["id"] for row in listing.json()] == [drawing_id]

    updated = await client.patch(
        f"/api/charts/drawings/{drawing_id}", headers=_headers(token),
        json={"points": [{"price": 1010.5}], "label": "新壓力"},
    )
    assert updated.status_code == 200
    assert updated.json()["points"][0]["price"] == 1010.5

    first_alert = await client.post(
        f"/api/charts/drawings/{drawing_id}/alert", headers=_headers(token),
        json={"condition": "above", "repeat": True, "cooldown_seconds": 3600},
    )
    assert first_alert.status_code == 200, first_alert.text
    assert first_alert.json()["condition_type"] == "price_above"
    assert first_alert.json()["target_price"] == 1010.5
    alert_id = first_alert.json()["id"]

    repeated = await client.post(
        f"/api/charts/drawings/{drawing_id}/alert", headers=_headers(token),
        json={"condition": "below"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["id"] == alert_id
    alerts = await client.get("/api/alerts", headers=_headers(token))
    assert len(alerts.json()) == 1

    deleted = await client.delete(f"/api/charts/drawings/{drawing_id}", headers=_headers(token))
    assert deleted.status_code == 204
    assert (await client.get("/api/charts/drawings/TW/2330", headers=_headers(token))).json() == []
    assert len((await client.get("/api/alerts", headers=_headers(token))).json()) == 1


@pytest.mark.asyncio
async def test_trend_validation_dynamic_alert_and_geometry_sync(client: AsyncClient, db_session):
    token = await _token(client, "drawing-trend@test.com")
    invalid = await client.post(
        "/api/charts/drawings", headers=_headers(token),
        json={"market": "US", "symbol": "AAPL", "kind": "trend", "points": [{"price": 100}]},
    )
    assert invalid.status_code == 422
    invalid_time = await client.post(
        "/api/charts/drawings", headers=_headers(token),
        json={
            "market": "US", "symbol": "AAPL", "kind": "trend",
            "points": [
                {"time": "not-a-date", "price": 100},
                {"time": "still-not-a-date", "price": 120},
            ],
        },
    )
    assert invalid_time.status_code == 422

    created = await client.post(
        "/api/charts/drawings", headers=_headers(token),
        json={
            "market": "US", "symbol": "AAPL", "kind": "trend",
            "points": [
                {"time": "2026-01-02", "price": 100},
                {"time": "2026-02-02", "price": 120},
            ],
        },
    )
    assert created.status_code == 201
    drawing_id = created.json()["id"]
    updated = await client.patch(
        f"/api/charts/drawings/{drawing_id}", headers=_headers(token),
        json={
            "points": [
                {"time": "2026-01-09", "price": 105},
                {"time": "2026-02-09", "price": 125},
            ],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["points"][1] == {"time": "2026-02-09", "price": 125.0}
    invalid_update = await client.patch(
        f"/api/charts/drawings/{drawing_id}", headers=_headers(token),
        json={
            "points": [
                {"time": "2026-01-09", "price": 105},
                {"time": "2026-01-09", "price": 125},
            ],
        },
    )
    assert invalid_update.status_code == 422
    null_update = await client.patch(
        f"/api/charts/drawings/{drawing_id}", headers=_headers(token),
        json={"points": None},
    )
    assert null_update.status_code == 422
    alert = await client.post(
        f"/api/charts/drawings/{drawing_id}/alert", headers=_headers(token),
        json={"condition": "above"},
    )
    assert alert.status_code == 200, alert.text
    assert alert.json()["condition_type"] == "trend_cross_above"
    assert alert.json()["target_price"] is None
    assert alert.json()["params"] == {
        "start_time": "2026-01-09", "start_price": 105.0,
        "end_time": "2026-02-09", "end_price": 125.0,
    }
    alert_id = alert.json()["id"]
    linked_model = await db_session.get(PriceAlert, uuid.UUID(alert_id))
    linked_model.runtime_state = {"trend_relation": "below"}
    await db_session.commit()
    repeated = await client.post(
        f"/api/charts/drawings/{drawing_id}/alert", headers=_headers(token),
        json={"condition": "below"},
    )
    assert repeated.json()["id"] == alert_id

    moved = await client.patch(
        f"/api/charts/drawings/{drawing_id}", headers=_headers(token),
        json={
            "points": [
                {"time": "2026-03-01", "price": 130},
                {"time": "2026-04-01", "price": 140},
            ],
        },
    )
    assert moved.status_code == 200
    (linked,) = (await client.get("/api/alerts", headers=_headers(token))).json()
    assert linked["id"] == alert_id
    assert linked["params"] == {
        "start_time": "2026-03-01", "start_price": 130.0,
        "end_time": "2026-04-01", "end_price": 140.0,
    }
    await db_session.refresh(linked_model)
    assert linked_model.runtime_state is None


@pytest.mark.asyncio
async def test_drawings_are_owner_scoped_and_require_auth(client: AsyncClient):
    owner = await _token(client, "drawing-owner@test.com")
    stranger = await _token(client, "drawing-stranger@test.com")
    created = await client.post(
        "/api/charts/drawings", headers=_headers(owner),
        json={"market": "CRYPTO", "symbol": "BTC", "kind": "horizontal", "points": [{"price": 80000}]},
    )
    drawing_id = created.json()["id"]
    assert (await client.get("/api/charts/drawings/CRYPTO/BTC")).status_code == 401
    assert (await client.get("/api/charts/drawings/CRYPTO/BTC", headers=_headers(stranger))).json() == []
    for method, suffix, payload in (
        (client.patch, "", {"label": "probe"}),
        (client.delete, "", None),
        (client.post, "/alert", {"condition": "above"}),
    ):
        response = await method(
            f"/api/charts/drawings/{drawing_id}{suffix}", headers=_headers(stranger),
            **({"json": payload} if payload is not None else {}),
        )
        assert response.status_code == 404
