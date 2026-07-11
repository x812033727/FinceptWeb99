"""P2 scheduler split: cross-worker subscription registry + alert pub/sub."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import api.websocket.manager as mgr
from config import settings


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


class FakeRedis:
    """Just enough of redis.asyncio for the registry paths."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

    async def set(self, key, value, ex=None, nx=None):
        self.store[key] = value
        return True

    async def mget(self, keys):
        return [self.store.get(k) for k in keys]

    async def publish(self, channel, payload):
        self.published.append((channel, payload))

    def scan_iter(self, match=None):
        prefix = (match or "").rstrip("*")
        keys = [k for k in self.store if k.startswith(prefix)]

        async def gen():
            for k in keys:
                yield k

        return gen()


# ── subscription mirror ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_mirror_subs_writes_worker_union():
    fake = FakeRedis()
    ws1, ws2 = FakeWS(), FakeWS()
    mgr._subscriptions[ws1] = {"AAPL:US", "2330:TW"}
    mgr._subscriptions[ws2] = {"BTC:CRYPTO"}
    try:
        with patch.object(mgr, "get_redis", AsyncMock(return_value=fake)):
            await mgr._mirror_subs()
        key = f"{mgr.SUBS_KEY_PREFIX}{mgr._WORKER_ID}"
        assert set(json.loads(fake.store[key])) == {"AAPL:US", "2330:TW", "BTC:CRYPTO"}
    finally:
        mgr._subscriptions.pop(ws1, None)
        mgr._subscriptions.pop(ws2, None)


@pytest.mark.asyncio
async def test_get_global_subscribed_unions_across_workers():
    fake = FakeRedis()
    fake.store[f"{mgr.SUBS_KEY_PREFIX}workerA"] = json.dumps(["AAPL:US", "2330:TW"])
    fake.store[f"{mgr.SUBS_KEY_PREFIX}workerB"] = json.dumps(["TSLA:US", "corrupt"])
    fake.store[f"{mgr.SUBS_KEY_PREFIX}workerC"] = "{not json"

    with patch.object(mgr, "get_redis", AsyncMock(return_value=fake)):
        us = await mgr.get_global_subscribed("US")
        tw = await mgr.get_global_subscribed("TW")

    assert us == {"AAPL", "TSLA"}   # cross-worker union, junk ignored
    assert tw == {"2330"}


@pytest.mark.asyncio
async def test_get_global_subscribed_falls_back_to_local_on_redis_error():
    ws = FakeWS()
    mgr._subscriptions[ws] = {"NVDA:US"}
    try:
        with patch.object(mgr, "get_redis", AsyncMock(side_effect=ConnectionError)):
            assert await mgr.get_global_subscribed("US") == {"NVDA"}
    finally:
        mgr._subscriptions.pop(ws, None)


# ── alert pub/sub ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_publish_alert_goes_through_alerts_channel():
    fake = FakeRedis()
    with patch.object(mgr, "get_redis", AsyncMock(return_value=fake)):
        await mgr.publish_alert_to_user("user-1", {"type": "alert", "symbol": "AAPL"})

    assert len(fake.published) == 1
    channel, raw = fake.published[0]
    assert channel == mgr.ALERTS_CHANNEL
    assert json.loads(raw) == {
        "user_id": "user-1",
        "data": {"type": "alert", "symbol": "AAPL"},
    }


@pytest.mark.asyncio
async def test_listener_routes_alert_channel_to_local_delivery():
    """A message arriving on user:alerts must go to _deliver_alert_local,
    not the market _dispatch path."""
    messages = [
        {
            "type": "message",
            "channel": mgr.ALERTS_CHANNEL,
            "data": json.dumps({"user_id": "u1", "data": {"type": "alert"}}),
        },
    ]

    def make_pubsub():
        pubsub = MagicMock()
        pubsub.subscribe = AsyncMock()
        pubsub.unsubscribe = AsyncMock()

        async def listen():
            for m in messages:
                yield m
            raise ConnectionError("end of test feed")

        pubsub.listen = listen
        return pubsub

    fake_redis = MagicMock()
    fake_redis.pubsub = make_pubsub
    delivered: list[tuple[str, dict]] = []

    async def fake_deliver(user_id, data):
        delivered.append((user_id, data))

    async def stop_loop(_delay):
        raise StopAsyncIteration  # break the reconnect loop after round 1

    with patch.object(mgr, "get_redis", AsyncMock(return_value=fake_redis)), \
         patch.object(mgr, "_deliver_alert_local", side_effect=fake_deliver), \
         patch.object(mgr, "_dispatch", AsyncMock()) as dispatch, \
         patch.object(mgr.asyncio, "sleep", side_effect=stop_loop):
        with pytest.raises(StopAsyncIteration):
            await mgr._listen_loop()

    assert delivered == [("u1", {"type": "alert"})]
    dispatch.assert_not_awaited()


# ── config gate ───────────────────────────────────────────────────

def test_scheduler_enabled_defaults_true():
    assert settings.SCHEDULER_ENABLED is True
