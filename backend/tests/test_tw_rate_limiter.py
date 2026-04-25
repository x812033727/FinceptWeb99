"""
Unit tests for the TWSE Redis token bucket rate limiter.

Verifies:
- `acquire_token` raises RedisUnavailable when Redis fails (caller falls back)
- `cache_decr` floors at min_value (used by AI quota refund)
- `_wait_for_token` returns False when Redis is down so the connector
  applies its local 1.1s pacing fallback
"""
from unittest.mock import AsyncMock, patch

import pytest

from cache.redis_cache import RedisUnavailable, acquire_token, cache_decr
from data.tw import twse_connector


@pytest.mark.asyncio
async def test_acquire_token_returns_true_on_lua_1():
    fake_redis = AsyncMock()
    fake_redis.script_load.return_value = "sha-abc"
    fake_redis.evalsha.return_value = 1
    with patch("cache.redis_cache.get_redis", return_value=fake_redis), \
         patch("cache.redis_cache._token_bucket_sha", None):
        assert await acquire_token("k", 1, 1.0) is True


@pytest.mark.asyncio
async def test_acquire_token_returns_false_when_bucket_empty():
    fake_redis = AsyncMock()
    fake_redis.script_load.return_value = "sha-abc"
    fake_redis.evalsha.return_value = 0
    with patch("cache.redis_cache.get_redis", return_value=fake_redis), \
         patch("cache.redis_cache._token_bucket_sha", None):
        assert await acquire_token("k", 1, 1.0) is False


@pytest.mark.asyncio
async def test_acquire_token_raises_when_redis_unreachable():
    fake_redis = AsyncMock()
    fake_redis.script_load.side_effect = ConnectionError("redis down")
    with patch("cache.redis_cache.get_redis", return_value=fake_redis), \
         patch("cache.redis_cache._token_bucket_sha", None):
        with pytest.raises(RedisUnavailable):
            await acquire_token("k", 1, 1.0)


@pytest.mark.asyncio
async def test_cache_decr_never_below_zero():
    fake_redis = AsyncMock()
    fake_redis.decr.return_value = -3
    fake_redis.set.return_value = True
    with patch("cache.redis_cache.get_redis", return_value=fake_redis):
        assert await cache_decr("k") == 0
        fake_redis.set.assert_awaited_once_with("k", 0)


@pytest.mark.asyncio
async def test_cache_decr_passes_through_positive_value():
    fake_redis = AsyncMock()
    fake_redis.decr.return_value = 7
    with patch("cache.redis_cache.get_redis", return_value=fake_redis):
        assert await cache_decr("k") == 7
        fake_redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_for_token_returns_false_on_redis_unavailable():
    async def boom(*_a, **_kw):
        raise RedisUnavailable("no redis")
    with patch("data.tw.twse_connector.acquire_token", side_effect=boom):
        used_redis = await twse_connector._wait_for_token()
    assert used_redis is False


@pytest.mark.asyncio
async def test_wait_for_token_returns_true_on_acquire():
    async def ok(*_a, **_kw):
        return True
    with patch("data.tw.twse_connector.acquire_token", side_effect=ok):
        used_redis = await twse_connector._wait_for_token()
    assert used_redis is True


@pytest.mark.asyncio
async def test_wait_for_token_polls_until_available(monkeypatch):
    calls = {"n": 0}

    async def flaky(*_a, **_kw):
        calls["n"] += 1
        return calls["n"] >= 3  # third call succeeds

    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("data.tw.twse_connector.acquire_token", flaky)
    monkeypatch.setattr("data.tw.twse_connector.asyncio.sleep", fake_sleep)

    used_redis = await twse_connector._wait_for_token()
    assert used_redis is True
    assert calls["n"] == 3
    assert sleeps == [0.1, 0.1]
