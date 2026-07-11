"""HTTP-surface tests for the Web Push subscription endpoints (PR-D3).

Transport delivery behavior (VAPID gate, 410 pruning, failed_count)
lives in test_web_push_service.py; only endpoint semantics live here:
upsert-by-endpoint, cross-account rebinding, user-scoped delete, and
the vapid-public-key configured/unconfigured contract.
"""
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from config import settings
from models.push_subscription import PushSubscription

ENDPOINT = "https://push.example.com/sub/abc123"
KEYS = {"p256dh": "BPubKeyMaterial", "auth": "AuthSecret"}


async def _auth_headers(client: AsyncClient, email: str) -> dict[str, str]:
    await client.post("/api/auth/register", json={
        "email": email,
        "password": "ValidPass99!",
    })
    r = await client.post("/api/auth/login", json={
        "email": email,
        "password": "ValidPass99!",
    })
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _sub_body(endpoint: str = ENDPOINT, **extra) -> dict:
    return {"endpoint": endpoint, "keys": KEYS, **extra}


# ── /vapid-public-key ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_vapid_key_unconfigured_by_default(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "")
    h = await _auth_headers(client, "vapid_unset@example.com")
    r = await client.get("/api/notifications/vapid-public-key", headers=h)
    assert r.status_code == 200
    assert r.json() == {"configured": False, "public_key": None}


@pytest.mark.asyncio
async def test_vapid_key_returned_when_configured(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "BTestPublicKey")
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "test-private")
    h = await _auth_headers(client, "vapid_set@example.com")
    r = await client.get("/api/notifications/vapid-public-key", headers=h)
    assert r.status_code == 200
    assert r.json() == {"configured": True, "public_key": "BTestPublicKey"}


@pytest.mark.asyncio
async def test_endpoints_require_auth(client: AsyncClient):
    assert (await client.get("/api/notifications/vapid-public-key")).status_code == 401
    assert (await client.post(
        "/api/notifications/push-subscribe", json=_sub_body(),
    )).status_code == 401


# ── subscribe (upsert by endpoint) ────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_creates_subscription(client: AsyncClient):
    h = await _auth_headers(client, "push_sub@example.com")
    r = await client.post(
        "/api/notifications/push-subscribe",
        json=_sub_body(user_agent="pytest-browser"),
        headers=h,
    )
    assert r.status_code == 201
    data = r.json()
    assert data["endpoint"] == ENDPOINT
    assert data["user_agent"] == "pytest-browser"
    assert data["failed_count"] == 0


@pytest.mark.asyncio
async def test_subscribe_upserts_by_endpoint(client: AsyncClient, db_session):
    """Re-subscribing from the same browser refreshes keys and resets
    failure bookkeeping instead of duplicating the row."""
    h = await _auth_headers(client, "push_upsert@example.com")
    await client.post("/api/notifications/push-subscribe", json=_sub_body(), headers=h)

    # Simulate accumulated delivery failures, then re-subscribe.
    sub = await db_session.scalar(
        select(PushSubscription).where(PushSubscription.endpoint == ENDPOINT))
    sub.failed_count = 3
    await db_session.commit()

    new_keys = {"p256dh": "BRotatedKey", "auth": "RotatedAuth"}
    r = await client.post(
        "/api/notifications/push-subscribe",
        json={"endpoint": ENDPOINT, "keys": new_keys},
        headers=h,
    )
    assert r.status_code == 201

    rows = (await db_session.execute(
        select(PushSubscription).where(PushSubscription.endpoint == ENDPOINT)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].keys == new_keys
    assert rows[0].failed_count == 0


@pytest.mark.asyncio
async def test_subscribe_rebinds_endpoint_to_new_user(client: AsyncClient, db_session):
    """A different account logging in on the same browser re-binds the
    endpoint — notifications must follow the signed-in user."""
    h_a = await _auth_headers(client, "push_owner_a@example.com")
    await client.post("/api/notifications/push-subscribe", json=_sub_body(), headers=h_a)

    h_b = await _auth_headers(client, "push_owner_b@example.com")
    r = await client.post("/api/notifications/push-subscribe", json=_sub_body(), headers=h_b)
    assert r.status_code == 201

    from models.user import User
    user_b = await db_session.scalar(
        select(User).where(User.email == "push_owner_b@example.com"))
    rows = (await db_session.execute(
        select(PushSubscription).where(PushSubscription.endpoint == ENDPOINT)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id == user_b.id


@pytest.mark.asyncio
async def test_subscribe_rejects_missing_keys(client: AsyncClient):
    h = await _auth_headers(client, "push_bad@example.com")
    r = await client.post(
        "/api/notifications/push-subscribe",
        json={"endpoint": ENDPOINT, "keys": {"p256dh": "only-half"}},
        headers=h,
    )
    assert r.status_code == 422


# ── unsubscribe (user-scoped) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_unsubscribe_deletes_own_subscription(client: AsyncClient, db_session):
    h = await _auth_headers(client, "push_del@example.com")
    await client.post("/api/notifications/push-subscribe", json=_sub_body(), headers=h)

    r = await client.request(
        "DELETE", "/api/notifications/push-subscribe",
        json={"endpoint": ENDPOINT}, headers=h,
    )
    assert r.status_code == 204
    assert await db_session.scalar(
        select(PushSubscription).where(PushSubscription.endpoint == ENDPOINT)
    ) is None


@pytest.mark.asyncio
async def test_unsubscribe_is_scoped_to_the_caller(client: AsyncClient, db_session):
    """User B cannot delete user A's subscription — 404, row survives."""
    h_a = await _auth_headers(client, "push_scope_a@example.com")
    await client.post("/api/notifications/push-subscribe", json=_sub_body(), headers=h_a)

    h_b = await _auth_headers(client, "push_scope_b@example.com")
    r = await client.request(
        "DELETE", "/api/notifications/push-subscribe",
        json={"endpoint": ENDPOINT}, headers=h_b,
    )
    assert r.status_code == 404
    assert await db_session.scalar(
        select(PushSubscription).where(PushSubscription.endpoint == ENDPOINT)
    ) is not None


@pytest.mark.asyncio
async def test_unsubscribe_unknown_endpoint_404s(client: AsyncClient):
    h = await _auth_headers(client, "push_del_none@example.com")
    r = await client.request(
        "DELETE", "/api/notifications/push-subscribe",
        json={"endpoint": "https://push.example.com/sub/never-existed"},
        headers=h,
    )
    assert r.status_code == 404
