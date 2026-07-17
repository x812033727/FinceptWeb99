"""D2 Email + LINE channel API and transport tests."""
import base64
import hashlib
import hmac
import json
import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from config import settings
from models.notification_channel import NotificationChannel
from services import channel_notification_service as svc


async def _auth(client: AsyncClient, email: str = "channels@example.com"):
    await client.post("/api/auth/register", json={"email": email, "password": "ValidPass99!"})
    login = await client.post("/api/auth/login", json={"email": email, "password": "ValidPass99!"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    me = await client.get("/api/auth/me", headers=headers)
    return headers, me.json()


def _smtp(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.test")
    monkeypatch.setattr(settings, "SMTP_FROM", "alerts@test.local")


def _line(monkeypatch):
    monkeypatch.setattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "channel-token")
    monkeypatch.setattr(settings, "LINE_CHANNEL_SECRET", "channel-secret")


@pytest.mark.asyncio
async def test_channel_list_has_virtual_defaults(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    monkeypatch.setattr(settings, "SMTP_FROM", "")
    monkeypatch.setattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "")
    monkeypatch.setattr(settings, "LINE_CHANNEL_SECRET", "")
    headers, _ = await _auth(client)

    response = await client.get("/api/notifications/channels", headers=headers)

    assert response.status_code == 200
    rows = {row["kind"]: row for row in response.json()}
    assert rows["email"]["configured"] is False
    assert rows["email"]["enabled"] is False
    assert rows["email"]["verified"] is True
    assert rows["email"]["daily_digest"] is False
    assert rows["email"]["destination_hint"].endswith("@example.com")
    assert rows["line"]["verified"] is False
    assert rows["line"]["event_kinds"] == [
        "price_alert", "strategy_health", "daily_picks_ready",
    ]


@pytest.mark.asyncio
async def test_email_channel_requires_provider_and_saves_filters(client: AsyncClient, monkeypatch):
    headers, _ = await _auth(client, "email-channel@example.com")
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    monkeypatch.setattr(settings, "SMTP_FROM", "")
    blocked = await client.put(
        "/api/notifications/channels/email", headers=headers,
        json={"enabled": True, "event_kinds": ["price_alert"], "daily_digest": True},
    )
    assert blocked.status_code == 409

    _smtp(monkeypatch)
    saved = await client.put(
        "/api/notifications/channels/email", headers=headers,
        json={"enabled": True, "event_kinds": ["price_alert"], "daily_digest": True},
    )
    assert saved.status_code == 200
    assert saved.json()["enabled"] is True
    assert saved.json()["verified"] is True
    assert saved.json()["event_kinds"] == ["price_alert"]
    assert saved.json()["daily_digest"] is True


@pytest.mark.asyncio
async def test_line_binding_rejects_bad_signature_then_consumes_token(client: AsyncClient, monkeypatch):
    _line(monkeypatch)
    headers, _ = await _auth(client, "line-bind@example.com")
    started = await client.post("/api/notifications/channels/line/bind", headers=headers)
    assert started.status_code == 200
    token = started.json()["token"]
    payload = {"events": [{
        "message": {"type": "text", "text": f"FINCEPT {token}"},
        "source": {"type": "user", "userId": "U0123456789"},
    }]}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    bad = await client.post(
        "/api/notifications/line/webhook", content=raw,
        headers={"x-line-signature": "bad", "content-type": "application/json"},
    )
    assert bad.status_code == 401

    malformed = b"[]"
    malformed_signature = base64.b64encode(hmac.new(
        b"channel-secret", malformed, hashlib.sha256,
    ).digest()).decode()
    invalid_shape = await client.post(
        "/api/notifications/line/webhook", content=malformed,
        headers={"x-line-signature": malformed_signature, "content-type": "application/json"},
    )
    assert invalid_shape.status_code == 400

    signature = base64.b64encode(hmac.new(
        b"channel-secret", raw, hashlib.sha256,
    ).digest()).decode()
    bound = await client.post(
        "/api/notifications/line/webhook", content=raw,
        headers={"x-line-signature": signature, "content-type": "application/json"},
    )
    assert bound.status_code == 200
    assert bound.json() == {"ok": True, "bound": 1}
    rows = (await client.get("/api/notifications/channels", headers=headers)).json()
    line = next(row for row in rows if row["kind"] == "line")
    assert line["verified"] is True
    assert line["enabled"] is True
    assert line["destination_hint"] == "LINE account connected"

    # Replaying the one-time token is a signed but harmless no-op.
    replay = await client.post(
        "/api/notifications/line/webhook", content=raw,
        headers={"x-line-signature": signature, "content-type": "application/json"},
    )
    assert replay.json()["bound"] == 0

    removed = await client.delete("/api/notifications/channels/line", headers=headers)
    assert removed.status_code == 204
    line = next(row for row in (
        await client.get("/api/notifications/channels", headers=headers)
    ).json() if row["kind"] == "line")
    assert line["verified"] is False
    assert line["enabled"] is False


@pytest.mark.asyncio
async def test_email_transport_respects_event_filter(client: AsyncClient, monkeypatch):
    _smtp(monkeypatch)
    headers, me = await _auth(client, "filter@example.com")
    await client.put(
        "/api/notifications/channels/email", headers=headers,
        json={"enabled": True, "event_kinds": ["strategy_health"]},
    )
    send = AsyncMock()
    monkeypatch.setattr(svc, "send_email", send)

    assert await svc.email_to_user(me["id"], {
        "type": "alert", "symbol": "2330", "message": "price",
    }) is False
    send.assert_not_awaited()

    assert await svc.email_to_user(me["id"], {
        "kind": "strategy_health_alert", "message": "degraded",
    }) is True
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_channel_payload_validation_and_auth(client: AsyncClient, monkeypatch):
    _smtp(monkeypatch)
    headers, _ = await _auth(client, "validate-channel@example.com")
    assert (await client.get("/api/notifications/channels")).status_code == 401
    empty = await client.put(
        "/api/notifications/channels/email", headers=headers,
        json={"enabled": False, "event_kinds": []},
    )
    assert empty.status_code == 422
    unknown = await client.put(
        "/api/notifications/channels/sms", headers=headers,
        json={"enabled": False, "event_kinds": ["price_alert"]},
    )
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_line_transport_uses_bound_destination_not_user_input(
    client: AsyncClient, db_session, monkeypatch,
):
    _line(monkeypatch)
    _headers, me = await _auth(client, "line-delivery@example.com")
    db_session.add(NotificationChannel(
        user_id=uuid.UUID(me["id"]), kind="line", enabled=True, verified=True,
        destination="U-safe-bound-id", config={"event_kinds": ["price_alert"]},
    ))
    await db_session.commit()
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return Response()

    monkeypatch.setattr(svc.httpx, "AsyncClient", Client)
    delivered = await svc.line_to_user(me["id"], {
        "type": "alert", "symbol": "AAPL", "market": "US",
        "message": "crossed", "to": "attacker-controlled",
    })

    assert delivered is True
    assert captured["url"] == svc.LINE_PUSH_URL
    assert captured["json"]["to"] == "U-safe-bound-id"
    assert captured["headers"]["Authorization"] == "Bearer channel-token"
    assert "attacker-controlled" not in captured["json"]["messages"][0]["text"]
