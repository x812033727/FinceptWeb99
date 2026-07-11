"""P1 stability batch: pubsub reconnect, snapshot MGET, screener warm lock."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import api.websocket.manager as mgr
from cache.redis_cache import cache_mget


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


# ── cache_mget ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cache_mget_empty_list_skips_redis():
    # Must not touch Redis at all — an empty MGET is a Redis error.
    with patch("cache.redis_cache.get_redis", side_effect=AssertionError):
        assert await cache_mget([]) == []


# ── _send_snapshot via MGET ───────────────────────────────────────

@pytest.mark.asyncio
async def test_send_snapshot_single_batch_read():
    ws = FakeWS()
    quotes = {
        "quote:us:AAPL": json.dumps({"price": 182.5}),
        "quote:tw:2330": None,  # cache miss — must be skipped, not sent
    }
    calls: list[list[str]] = []

    async def fake_mget(keys: list[str]) -> list:
        calls.append(keys)
        return [quotes.get(k) for k in keys]

    with patch("cache.redis_cache.cache_mget", side_effect=fake_mget), \
         patch("cache.redis_cache.key_quote",
               side_effect=lambda m, s: f"quote:{m}:{s}"):
        await mgr._send_snapshot(ws, {"AAPL:US", "2330:TW"})

    assert len(calls) == 1          # exactly one round-trip
    assert len(calls[0]) == 2
    assert ws.sent[0]["type"] == "snapshot"
    assert ws.sent[0]["data"] == {"AAPL:US": {"price": 182.5}}


@pytest.mark.asyncio
async def test_send_snapshot_corrupt_entry_does_not_sink_others():
    ws = FakeWS()

    async def fake_mget(keys: list[str]) -> list:
        return ["{not json", json.dumps({"price": 1.0})]

    with patch("cache.redis_cache.cache_mget", side_effect=fake_mget), \
         patch("cache.redis_cache.key_quote",
               side_effect=lambda m, s: f"quote:{m}:{s}"):
        await mgr._send_snapshot(ws, {"AAA:US", "BBB:US"})

    assert ws.sent[0]["type"] == "snapshot"
    assert len(ws.sent[0]["data"]) == 1


# ── pubsub listener reconnect ─────────────────────────────────────

@pytest.mark.asyncio
async def test_listen_loop_reconnects_after_connection_drop():
    """listen() raising must re-enter the loop (with backoff), not die."""
    attempts = []

    def make_pubsub():
        pubsub = MagicMock()
        pubsub.subscribe = AsyncMock()
        pubsub.unsubscribe = AsyncMock()

        async def listen():
            attempts.append(1)
            raise ConnectionError("redis dropped")
            yield  # pragma: no cover — makes this an async generator

        pubsub.listen = listen
        return pubsub

    fake_redis = MagicMock()
    fake_redis.pubsub = make_pubsub

    async def fake_sleep(_delay):
        if len(attempts) >= 2:      # two failed subscribes → stop the test
            raise asyncio.CancelledError

    before = mgr.WS_PUBSUB_RECONNECTS_TOTAL._value.get()
    with patch.object(mgr, "get_redis", AsyncMock(return_value=fake_redis)), \
         patch.object(mgr.asyncio, "sleep", side_effect=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await mgr._listen_loop()

    assert len(attempts) == 2       # it DID retry after the first drop
    assert mgr.WS_PUBSUB_RECONNECTS_TOTAL._value.get() == before + 2


# ── screener warm lock ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_us_screener_skips_when_lock_held():
    from tasks.us_market_refresh import refresh_us_screener

    with patch("cache.redis_cache.acquire_lock",
               AsyncMock(return_value=False)), \
         patch("services.us_market_service.get_screener",
               AsyncMock()) as get_screener:
        await refresh_us_screener()

    get_screener.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_us_screener_runs_when_lock_acquired():
    from tasks.us_market_refresh import refresh_us_screener

    with patch("cache.redis_cache.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("services.us_market_service.get_screener",
               AsyncMock()) as get_screener:
        await refresh_us_screener()

    get_screener.assert_awaited_once()
