"""Integration tests for price alert CRUD HTTP endpoints.

check_and_fire logic is covered comprehensively (19 tests, boundary
cases, multi-user fan-out, double-fire guard, market mismatch) in
test_alert_service.py — as pure unit tests that don't require the
FastAPI test client. Only HTTP-surface behavior lives here.
"""
import pytest
from httpx import AsyncClient


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


# ── /api/alerts/history (PR-D5) ──────────────────────────────────────

async def _seed_events(db_session, email: str, symbols: list[str]):
    """Insert alert_events rows (one per symbol, 1 min apart) for the
    registered user with this email."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from models.alert import AlertEvent
    from models.user import User

    user = await db_session.scalar(select(User).where(User.email == email))
    base = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    for i, sym in enumerate(symbols):
        db_session.add(AlertEvent(
            user_id=user.id, symbol=sym, market="US", kind="price",
            message=f"{sym} fired", fired_at=base + timedelta(minutes=i),
        ))
    await db_session.commit()


@pytest.mark.asyncio
async def test_history_empty(client: AsyncClient):
    h = await _auth_headers(client, "hist_empty@example.com")
    r = await client.get("/api/alerts/history", headers=h)
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_history_requires_auth(client: AsyncClient):
    r = await client.get("/api/alerts/history")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_history_newest_first_and_paginated(client: AsyncClient, db_session):
    h = await _auth_headers(client, "hist_page@example.com")
    await _seed_events(db_session, "hist_page@example.com", ["A1", "A2", "A3"])

    r = await client.get("/api/alerts/history?limit=2", headers=h)
    assert r.status_code == 200
    page1 = r.json()
    assert [e["symbol"] for e in page1] == ["A3", "A2"]

    r = await client.get(
        f"/api/alerts/history?limit=2&before={page1[-1]['fired_at']}",
        headers=h,
    )
    page2 = r.json()
    assert [e["symbol"] for e in page2] == ["A1"]


@pytest.mark.asyncio
async def test_history_no_cross_user_leak(client: AsyncClient, db_session):
    h1 = await _auth_headers(client, "hist_u1@example.com")
    h2 = await _auth_headers(client, "hist_u2@example.com")
    await _seed_events(db_session, "hist_u1@example.com", ["OWN1"])

    r1 = (await client.get("/api/alerts/history", headers=h1)).json()
    r2 = (await client.get("/api/alerts/history", headers=h2)).json()
    assert [e["symbol"] for e in r1] == ["OWN1"]
    assert r2 == []


@pytest.mark.asyncio
async def test_history_limit_validation(client: AsyncClient):
    h = await _auth_headers(client, "hist_lim@example.com")
    r = await client.get("/api/alerts/history?limit=0", headers=h)
    assert r.status_code == 422
    r = await client.get("/api/alerts/history?limit=500", headers=h)
    assert r.status_code == 422


# ── rule engine surface (PR-D1) ──────────────────────────────────

@pytest.mark.asyncio
async def test_create_rule_alert_with_params(client: AsyncClient):
    h = await _auth_headers(client, "rule_create@example.com")
    r = await client.post("/api/alerts", json={
        "symbol": "NVDA",
        "market": "US",
        "condition_type": "pct_change_above",
        "params": {"pct": 5.0},
        "repeat": True,
        "cooldown_seconds": 3600,
    }, headers=h)
    assert r.status_code == 201
    data = r.json()
    assert data["condition_type"] == "pct_change_above"
    assert data["params"] == {"pct": 5.0}
    assert data["repeat"] is True
    assert data["cooldown_seconds"] == 3600
    assert data["condition"] is None
    assert data["target_price"] is None
    assert data["last_fired_at"] is None


@pytest.mark.asyncio
async def test_create_rule_alert_fills_param_defaults(client: AsyncClient):
    h = await _auth_headers(client, "rule_defaults@example.com")
    r = await client.post("/api/alerts", json={
        "symbol": "2330",
        "market": "TW",
        "condition_type": "volume_surge",
    }, headers=h)
    assert r.status_code == 201
    assert r.json()["params"] == {"multiple": 2.0, "lookback_days": 20}


@pytest.mark.asyncio
async def test_create_unknown_condition_type_422(client: AsyncClient):
    h = await _auth_headers(client, "rule_unknown@example.com")
    r = await client.post("/api/alerts", json={
        "symbol": "AAPL",
        "market": "US",
        "condition_type": "rsi_cross",   # not implemented
        "params": {"level": 70},
    }, headers=h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_bad_params_422(client: AsyncClient):
    h = await _auth_headers(client, "rule_badparams@example.com")
    # missing required pct
    r = await client.post("/api/alerts", json={
        "symbol": "AAPL", "market": "US",
        "condition_type": "pct_change_above", "params": {},
    }, headers=h)
    assert r.status_code == 422
    # unknown param key (extra=forbid)
    r = await client.post("/api/alerts", json={
        "symbol": "AAPL", "market": "US",
        "condition_type": "breakout_high", "params": {"pct": 5},
    }, headers=h)
    assert r.status_code == 422
    # out-of-range lookback
    r = await client.post("/api/alerts", json={
        "symbol": "AAPL", "market": "US",
        "condition_type": "breakout_high", "params": {"lookback_days": 1},
    }, headers=h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_price_rule_requires_target_price(client: AsyncClient):
    h = await _auth_headers(client, "rule_notarget@example.com")
    r = await client.post("/api/alerts", json={
        "symbol": "AAPL", "market": "US", "condition_type": "price_above",
    }, headers=h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_non_price_rule_rejects_target_price(client: AsyncClient):
    h = await _auth_headers(client, "rule_target_mix@example.com")
    r = await client.post("/api/alerts", json={
        "symbol": "AAPL", "market": "US",
        "condition_type": "pct_change_above",
        "params": {"pct": 5}, "target_price": 100.0,
    }, headers=h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_streak_alert_tw_only(client: AsyncClient):
    h = await _auth_headers(client, "rule_twonly@example.com")
    r = await client.post("/api/alerts", json={
        "symbol": "AAPL", "market": "US",
        "condition_type": "foreign_net_buy_streak", "params": {"days": 3},
    }, headers=h)
    assert r.status_code == 422
    r = await client.post("/api/alerts", json={
        "symbol": "2330", "market": "TW",
        "condition_type": "foreign_net_buy_streak", "params": {"days": 3},
    }, headers=h)
    assert r.status_code == 201
    assert r.json()["condition_type"] == "foreign_net_buy_streak"


@pytest.mark.asyncio
async def test_legacy_create_maps_to_condition_type(client: AsyncClient):
    """Pre-D1 payload shape keeps working and lands on price_above."""
    h = await _auth_headers(client, "rule_legacy@example.com")
    r = await client.post("/api/alerts", json={
        "symbol": "AAPL", "market": "US",
        "condition": "above", "target_price": 200.0,
    }, headers=h)
    assert r.status_code == 201
    data = r.json()
    assert data["condition_type"] == "price_above"
    assert data["condition"] == "above"
    assert data["repeat"] is False
    assert data["cooldown_seconds"] == 0


@pytest.mark.asyncio
async def test_list_returns_rule_fields(client: AsyncClient):
    h = await _auth_headers(client, "rule_list@example.com")
    await client.post("/api/alerts", json={
        "symbol": "2330", "market": "TW",
        "condition_type": "breakout_high", "params": {"lookback_days": 60},
        "repeat": True, "cooldown_seconds": 86400,
    }, headers=h)
    r = await client.get("/api/alerts", headers=h)
    assert r.status_code == 200
    (row,) = r.json()
    assert row["condition_type"] == "breakout_high"
    assert row["params"] == {"lookback_days": 60}
    assert row["repeat"] is True
    assert row["cooldown_seconds"] == 86400


@pytest.mark.asyncio
async def test_patch_updates_rule_knobs(client: AsyncClient):
    h = await _auth_headers(client, "rule_patch@example.com")
    created = (await client.post("/api/alerts", json={
        "symbol": "AAPL", "market": "US",
        "condition_type": "pct_change_above", "params": {"pct": 5.0},
    }, headers=h)).json()

    r = await client.patch(f"/api/alerts/{created['id']}", json={
        "params": {"pct": 7.5}, "repeat": True, "cooldown_seconds": 300,
    }, headers=h)
    assert r.status_code == 200
    data = r.json()
    assert data["params"] == {"pct": 7.5}
    assert data["repeat"] is True
    assert data["cooldown_seconds"] == 300


@pytest.mark.asyncio
async def test_patch_bad_params_422(client: AsyncClient):
    h = await _auth_headers(client, "rule_patch_bad@example.com")
    created = (await client.post("/api/alerts", json={
        "symbol": "AAPL", "market": "US",
        "condition_type": "pct_change_above", "params": {"pct": 5.0},
    }, headers=h)).json()
    r = await client.patch(f"/api/alerts/{created['id']}", json={
        "params": {"lookback_days": 20},
    }, headers=h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_nonexistent_404(client: AsyncClient):
    import uuid
    h = await _auth_headers(client, "rule_patch_404@example.com")
    r = await client.patch(f"/api/alerts/{uuid.uuid4()}", json={
        "repeat": True,
    }, headers=h)
    assert r.status_code == 404
