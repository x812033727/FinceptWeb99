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


@pytest.mark.asyncio
async def test_wait_for_token_falls_back_to_local_pacing_on_starvation(monkeypatch):
    """Regression: bucket starvation used to return True (i.e.
    "Redis-acquired token, skip local pacing"), which silently
    bypassed ALL rate limiting after 30s of starvation. The fix
    returns False so the caller still applies the local 1.1s sleep —
    slower under sustained pressure but won't spam TWSE.
    """
    async def always_empty(*_a, **_kw):
        return False  # bucket has zero tokens forever

    async def fake_sleep(_s: float) -> None:
        pass  # collapse the 30s wait into a tight loop

    monkeypatch.setattr("data.tw.twse_connector.acquire_token", always_empty)
    monkeypatch.setattr("data.tw.twse_connector.asyncio.sleep", fake_sleep)

    used_redis = await twse_connector._wait_for_token()
    assert used_redis is False


@pytest.mark.asyncio
async def test_wait_for_token_increments_starvation_counter(monkeypatch):
    """The bucket-starvation counter must fire exactly once per fall-
    open event so operators can alert on sustained pressure."""
    from middleware.metrics import TWSE_RATE_LIMIT_DEGRADED_TOTAL

    async def always_empty(*_a, **_kw):
        return False

    async def fake_sleep(_s: float) -> None:
        pass

    monkeypatch.setattr("data.tw.twse_connector.acquire_token", always_empty)
    monkeypatch.setattr("data.tw.twse_connector.asyncio.sleep", fake_sleep)

    before = TWSE_RATE_LIMIT_DEGRADED_TOTAL.labels(reason="bucket_starvation")._value.get()
    await twse_connector._wait_for_token()
    after = TWSE_RATE_LIMIT_DEGRADED_TOTAL.labels(reason="bucket_starvation")._value.get()
    assert after == before + 1


@pytest.mark.asyncio
async def test_wait_for_token_increments_redis_unavailable_counter():
    """The Redis-unavailable counter is the leading signal that the
    cross-pod coordination has dropped — distinct from bucket
    starvation, which is sustained TWSE pressure with Redis healthy.
    """
    from middleware.metrics import TWSE_RATE_LIMIT_DEGRADED_TOTAL

    async def boom(*_a, **_kw):
        raise RedisUnavailable("connection refused")

    before = TWSE_RATE_LIMIT_DEGRADED_TOTAL.labels(reason="redis_unavailable")._value.get()
    with patch("data.tw.twse_connector.acquire_token", side_effect=boom):
        result = await twse_connector._wait_for_token()
    after = TWSE_RATE_LIMIT_DEGRADED_TOTAL.labels(reason="redis_unavailable")._value.get()
    assert result is False
    assert after == before + 1


@pytest.mark.asyncio
async def test_wait_for_token_does_not_increment_counter_on_success():
    """The counter must stay flat when Redis returns a token on the
    first try — otherwise the dashboard can't tell baseline noise
    from real degradation."""
    from middleware.metrics import TWSE_RATE_LIMIT_DEGRADED_TOTAL

    async def ok(*_a, **_kw):
        return True

    redis_before = TWSE_RATE_LIMIT_DEGRADED_TOTAL.labels(reason="redis_unavailable")._value.get()
    starve_before = TWSE_RATE_LIMIT_DEGRADED_TOTAL.labels(reason="bucket_starvation")._value.get()
    with patch("data.tw.twse_connector.acquire_token", side_effect=ok):
        result = await twse_connector._wait_for_token()
    assert result is True
    assert TWSE_RATE_LIMIT_DEGRADED_TOTAL.labels(reason="redis_unavailable")._value.get() == redis_before
    assert TWSE_RATE_LIMIT_DEGRADED_TOTAL.labels(reason="bucket_starvation")._value.get() == starve_before
