"""Unit tests for services.web_push_service (PR-D3 web_push transport).

pywebpush is mocked at the module seam (`web_push_service.webpush`) —
these tests pin down the transport's contract, not the wire protocol:

  - Fail-closed VAPID gate: unset keys → no send attempt, no error
    (same shape as email_service.is_configured()).
  - Fan-out: one send per subscription the user holds; other users'
    subscriptions are never touched.
  - Subscription hygiene: HTTP 404/410 → prune immediately; other
    failures increment failed_count and prune at the threshold;
    success stamps last_success_at and resets the counter.
  - Payload contract: the JSON handed to pywebpush carries the
    title/body/tag/url shape the service worker's push handler renders.
"""
import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pywebpush import WebPushException
from sqlalchemy import select

import services.web_push_service as wps
from config import settings
from models.push_subscription import PushSubscription
from models.user import User

ALERT_PAYLOAD = {
    "type": "alert",
    "id": "11111111-1111-1111-1111-111111111111",
    "symbol": "2330",
    "market": "TW",
    "message": "2330 突破 1000 元",
}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "BTestPublicKey")
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "test-private-key")
    monkeypatch.setattr(settings, "VAPID_CLAIMS_EMAIL", "ops@example.com")


@pytest.fixture
def webpush_mock(monkeypatch):
    mock = MagicMock(return_value=None)
    monkeypatch.setattr(wps, "webpush", mock)
    return mock


async def _seed_user_with_subs(db, email: str, endpoints: list[str]) -> User:
    user = User(email=email, hashed_password="x")
    db.add(user)
    await db.flush()
    for ep in endpoints:
        db.add(PushSubscription(
            user_id=user.id, endpoint=ep,
            keys={"p256dh": "BKey", "auth": "Auth"},
        ))
    await db.commit()
    return user


def _gone(status: int) -> WebPushException:
    return WebPushException(
        f"Push failed: {status}", response=SimpleNamespace(status_code=status))


# ── VAPID fail-closed gate ────────────────────────────────────────

def test_is_configured_requires_both_keys(monkeypatch):
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "")
    assert wps.is_configured() is False
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "pub")
    assert wps.is_configured() is False
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "priv")
    assert wps.is_configured() is True


@pytest.mark.asyncio
async def test_push_skips_silently_when_vapid_unset(
    db_session, webpush_mock, monkeypatch,
):
    """Default settings (empty VAPID keys): the transport must return
    without touching the DB or attempting a send — an unconfigured
    deployment's alert cron never pays for web push."""
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "")
    user = await _seed_user_with_subs(
        db_session, "unconf@example.com", ["https://push.example.com/s/1"])

    await wps.push_to_user(str(user.id), ALERT_PAYLOAD)

    webpush_mock.assert_not_called()


# ── delivery fan-out + payload contract ──────────────────────────

@pytest.mark.asyncio
async def test_push_sends_to_every_subscription_of_the_user(
    db_session, configured, webpush_mock,
):
    user = await _seed_user_with_subs(db_session, "fanout@example.com", [
        "https://push.example.com/s/desktop",
        "https://push.example.com/s/phone",
    ])
    await _seed_user_with_subs(
        db_session, "other@example.com", ["https://push.example.com/s/other"])

    await wps.push_to_user(str(user.id), ALERT_PAYLOAD)

    assert webpush_mock.call_count == 2
    sent_endpoints = {
        c.kwargs["subscription_info"]["endpoint"] for c in webpush_mock.call_args_list
    }
    assert sent_endpoints == {
        "https://push.example.com/s/desktop",
        "https://push.example.com/s/phone",
    }

    # Payload contract the sw.js push handler renders.
    body = json.loads(webpush_mock.call_args.kwargs["data"])
    assert body["title"] == "2330 (TW) 告警"
    assert body["body"] == "2330 突破 1000 元"
    assert body["tag"] == f"fincept-alert-{ALERT_PAYLOAD['id']}"
    assert body["url"] == "/alerts"
    assert webpush_mock.call_args.kwargs["vapid_private_key"] == "test-private-key"
    assert webpush_mock.call_args.kwargs["vapid_claims"] == {
        "sub": "mailto:ops@example.com"}


@pytest.mark.asyncio
async def test_success_stamps_last_success_and_resets_failed_count(
    db_session, configured, webpush_mock,
):
    user = await _seed_user_with_subs(
        db_session, "ok@example.com", ["https://push.example.com/s/ok"])
    sub = await db_session.scalar(select(PushSubscription))
    sub.failed_count = 3
    await db_session.commit()

    await wps.push_to_user(str(user.id), ALERT_PAYLOAD)

    db_session.expire_all()
    sub = await db_session.scalar(select(PushSubscription))
    assert sub.failed_count == 0
    assert sub.last_success_at is not None


# ── pruning ───────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 410])
async def test_gone_response_prunes_subscription(
    db_session, configured, webpush_mock, status,
):
    user = await _seed_user_with_subs(
        db_session, f"gone{status}@example.com", ["https://push.example.com/s/gone"])
    webpush_mock.side_effect = _gone(status)

    await wps.push_to_user(str(user.id), ALERT_PAYLOAD)

    db_session.expire_all()
    assert await db_session.scalar(select(PushSubscription)) is None


@pytest.mark.asyncio
async def test_other_failures_increment_failed_count(
    db_session, configured, webpush_mock,
):
    user = await _seed_user_with_subs(
        db_session, "flaky@example.com", ["https://push.example.com/s/flaky"])
    webpush_mock.side_effect = _gone(500)

    await wps.push_to_user(str(user.id), ALERT_PAYLOAD)

    db_session.expire_all()
    sub = await db_session.scalar(select(PushSubscription))
    assert sub is not None            # kept — under the prune threshold
    assert sub.failed_count == 1


@pytest.mark.asyncio
async def test_prunes_after_max_consecutive_failures(
    db_session, configured, webpush_mock,
):
    user = await _seed_user_with_subs(
        db_session, "dead@example.com", ["https://push.example.com/s/dead"])
    webpush_mock.side_effect = _gone(500)

    for _ in range(wps.MAX_CONSECUTIVE_FAILURES):
        await wps.push_to_user(str(user.id), ALERT_PAYLOAD)

    db_session.expire_all()
    assert await db_session.scalar(select(PushSubscription)) is None


@pytest.mark.asyncio
async def test_one_dead_subscription_does_not_block_the_rest(
    db_session, configured, webpush_mock,
):
    """A 410 on the first subscription must not stop delivery to the
    user's other browsers."""
    user = await _seed_user_with_subs(db_session, "mixed@example.com", [
        "https://push.example.com/s/a-dead",
        "https://push.example.com/s/b-live",
    ])

    def side_effect(**kwargs):
        if kwargs["subscription_info"]["endpoint"].endswith("a-dead"):
            raise _gone(410)

    webpush_mock.side_effect = side_effect

    await wps.push_to_user(str(user.id), ALERT_PAYLOAD)

    assert webpush_mock.call_count == 2
    db_session.expire_all()
    remaining = (await db_session.execute(select(PushSubscription))).scalars().all()
    assert [s.endpoint for s in remaining] == ["https://push.example.com/s/b-live"]
    assert remaining[0].last_success_at is not None


@pytest.mark.asyncio
async def test_invalid_user_id_is_a_noop(configured, webpush_mock):
    await wps.push_to_user("not-a-uuid", ALERT_PAYLOAD)
    webpush_mock.assert_not_called()


@pytest.mark.asyncio
async def test_user_without_subscriptions_is_a_noop(
    db_session, configured, webpush_mock,
):
    await wps.push_to_user(str(uuid.uuid4()), ALERT_PAYLOAD)
    webpush_mock.assert_not_called()
