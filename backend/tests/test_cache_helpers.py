"""Pure unit tests for the JSON cache helpers in cache.redis_cache.

The helpers (`cache_get_json`, `cache_set_json`,
`cache_set_json_unless_empty`) fold the json.dumps / json.loads pair
that was repeated across every service's get_quote / get_history
read path. These tests guard the three contracts services rely on:

  - decode-error tolerance on read (don't 500 on poisoned cache)
  - empty-guard on write (don't lock the next request into 15s
    of an empty payload)
  - falsy values include None / [] / {} / "" / 0
"""
import json
from unittest.mock import AsyncMock, patch

import pytest
from prometheus_client import REGISTRY

from cache import redis_cache as rc
from middleware.metrics import CACHE_HITS_TOTAL


@pytest.mark.asyncio
async def test_cache_get_json_returns_parsed_value_on_hit():
    with patch.object(rc, "cache_get", AsyncMock(return_value='{"price": 100}')):
        result = await rc.cache_get_json("k")
    assert result == {"price": 100}


@pytest.mark.asyncio
async def test_cache_get_json_returns_none_on_miss():
    with patch.object(rc, "cache_get", AsyncMock(return_value=None)):
        result = await rc.cache_get_json("k")
    assert result is None


@pytest.mark.asyncio
async def test_cache_get_json_returns_none_on_decode_error():
    """One corrupted Redis value should make the caller refetch
    upstream rather than 500 the request."""
    with patch.object(rc, "cache_get", AsyncMock(return_value="{not valid json")):
        result = await rc.cache_get_json("k")
    assert result is None


@pytest.mark.asyncio
async def test_cache_set_json_serializes_and_stores():
    mock_set = AsyncMock()
    with patch.object(rc, "cache_set", mock_set):
        await rc.cache_set_json("k", {"a": 1, "b": [2, 3]}, ttl_seconds=60)
    mock_set.assert_awaited_once()
    args = mock_set.call_args.args
    assert args[0] == "k"
    assert json.loads(args[1]) == {"a": 1, "b": [2, 3]}
    assert args[2] == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("falsy", [None, [], {}, "", 0])
async def test_cache_set_json_unless_empty_skips_falsy(falsy):
    mock_set = AsyncMock()
    with patch.object(rc, "cache_set", mock_set):
        await rc.cache_set_json_unless_empty("k", falsy, ttl_seconds=60)
    mock_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_set_json_unless_empty_writes_truthy():
    mock_set = AsyncMock()
    with patch.object(rc, "cache_set", mock_set):
        await rc.cache_set_json_unless_empty("k", [{"x": 1}], ttl_seconds=60)
    mock_set.assert_awaited_once()


# ── Prometheus instrumentation ─────────────────────────────────────


@pytest.mark.parametrize(
    "key,expected",
    [
        ("tw:quote:2330:realtime",        "tw.quote"),
        ("us:financials:AAPL:annual",     "us.financials"),
        ("crypto:news:BTC",               "crypto.news"),
        ("tw:screener:TWSE:1000:None:…",  "tw.screener"),
        ("tw:fundamentals:2330",          "tw.fundamentals"),
        # 1-segment fallbacks
        ("github:release:latest",         "github.release"),
        ("standalone_key",                "standalone_key"),
        # Defensive
        ("",                              "unknown"),
        (":leading_colon",                "unknown"),
    ],
)
def test_endpoint_label_pulls_market_and_datatype(key, expected):
    """The label collapses to the first two colon-segments so every
    new builder adds at most one new endpoint to the Prometheus
    label-set."""
    assert rc._endpoint_label(key) == expected


def test_tw_health_cache_key_versions_and_separates_period_windows():
    assert rc.key_health_tw("2330", 4) == "tw:health:v2:2330:4"
    assert rc.key_health_tw("2330", 8) != rc.key_health_tw("2330", 4)


def test_factor_cache_keys_include_every_result_shaping_parameter():
    assert rc.key_factor_ranking_tw("2025-06-30", "value", 50) == (
        "tw:factor_ranking:v8:2025-06-30:value:50:1"
    )
    first = rc.key_factor_validation_tw("2024-01-01", "2025-01-01", "balanced", 20, 21, 20)
    changed_cost = rc.key_factor_validation_tw("2024-01-01", "2025-01-01", "balanced", 20, 21, 30)
    assert first != changed_cost
    changed_notional = rc.key_factor_validation_tw(
        "2024-01-01", "2025-01-01", "balanced", 20, 21, 20,
        portfolio_notional_twd=50_000_000,
    )
    assert first != changed_notional
    assert first != rc.key_factor_validation_tw(
        "2024-01-01", "2025-01-01", "balanced", 20, 21, 20,
        benchmark="equal_weight",
    )
    assert first != rc.key_factor_validation_tw(
        "2024-01-01", "2025-01-01", "balanced", 20, 21, 20,
        weight_mode="fixed",
    )
    assert rc.key_factor_ranking_tw("2025-06-30", "value", 50, True) != (
        rc.key_factor_ranking_tw("2025-06-30", "value", 50, False)
    )
    assert rc.key_factor_ranking_tw(
        "2025-06-30", "value", 50, model_key="model-a",
    ) != rc.key_factor_ranking_tw(
        "2025-06-30", "value", 50, model_key="model-b",
    )


@pytest.mark.asyncio
async def test_cache_get_json_increments_hit_counter():
    label = "tw.quote"
    before = REGISTRY.get_sample_value("cache_hits_total", {"endpoint": label}) or 0
    with patch.object(rc, "cache_get", AsyncMock(return_value='{"price": 100}')):
        result = await rc.cache_get_json("tw:quote:2330:realtime")
    after = REGISTRY.get_sample_value("cache_hits_total", {"endpoint": label}) or 0

    assert result == {"price": 100}
    assert after == before + 1
    # Sanity: the counter handle exposes the same value via the
    # public Counter API (guards against the registry helper being
    # patched or the metric being re-registered under a different ns).
    assert CACHE_HITS_TOTAL.labels(endpoint=label)._value.get() == after


@pytest.mark.asyncio
async def test_cache_get_json_increments_miss_counter_on_empty():
    from prometheus_client import REGISTRY

    label = "tw.history"
    before = REGISTRY.get_sample_value("cache_misses_total", {"endpoint": label}) or 0
    with patch.object(rc, "cache_get", AsyncMock(return_value=None)):
        result = await rc.cache_get_json("tw:history:2330:1d:1y")
    after = REGISTRY.get_sample_value("cache_misses_total", {"endpoint": label}) or 0

    assert result is None
    assert after == before + 1


@pytest.mark.asyncio
async def test_cache_get_json_increments_miss_counter_on_decode_error():
    """Poisoned cache surfaces as None to the caller; the metric should
    reflect the same — operators reasoning about "how often did we
    refetch upstream" don't want to distinguish empty from corrupt."""
    from prometheus_client import REGISTRY

    label = "us.options"
    before = REGISTRY.get_sample_value("cache_misses_total", {"endpoint": label}) or 0
    with patch.object(rc, "cache_get", AsyncMock(return_value="{garbage")):
        result = await rc.cache_get_json("us:options:AAPL:2026-06-20")
    after = REGISTRY.get_sample_value("cache_misses_total", {"endpoint": label}) or 0

    assert result is None
    assert after == before + 1


# ── Per-dataset usage hash helpers (FinMind cutover instrumentation) ──


@pytest.mark.asyncio
async def test_cache_hincrby_returns_new_field_value():
    fake_redis = AsyncMock()
    fake_redis.hincrby.return_value = 7
    with patch("cache.redis_cache.get_redis", return_value=fake_redis):
        assert await rc.cache_hincrby("h", "TaiwanStockPrice", 1) == 7
    fake_redis.hincrby.assert_awaited_once_with("h", "TaiwanStockPrice", 1)


@pytest.mark.asyncio
async def test_cache_hincrby_arms_ttl_only_on_first_field_write():
    """Expiry is armed once, when the field's returned value equals the
    increment (a fresh hash's first field). Later bumps must not extend
    the day-hash's TTL — otherwise a busy dataset keeps the hash alive
    forever and it never self-clears."""
    fake_redis = AsyncMock()
    fake_redis.hincrby.return_value = 1  # first write → value == amount
    with patch("cache.redis_cache.get_redis", return_value=fake_redis):
        await rc.cache_hincrby("h", "d", 1, ttl_seconds=3600)
    fake_redis.expire.assert_awaited_once_with("h", 3600)

    fake_redis.expire.reset_mock()
    fake_redis.hincrby.return_value = 5  # subsequent write → value > amount
    with patch("cache.redis_cache.get_redis", return_value=fake_redis):
        await rc.cache_hincrby("h", "d", 1, ttl_seconds=3600)
    fake_redis.expire.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_hgetall_returns_hash_dict():
    fake_redis = AsyncMock()
    fake_redis.hgetall.return_value = {"TaiwanStockPrice": "42", "TaiwanStockPER": "9"}
    with patch("cache.redis_cache.get_redis", return_value=fake_redis):
        out = await rc.cache_hgetall("h")
    assert out == {"TaiwanStockPrice": "42", "TaiwanStockPER": "9"}


def test_key_finmind_dataset_usage_shape():
    assert rc.key_finmind_dataset_usage("20260712") == "finmind:upstream:usage:20260712"
