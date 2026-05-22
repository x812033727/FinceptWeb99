"""Tests for the Redis-backed TW symbol-map cache.

Without persistence the in-memory ``_name_map`` resets to ``{}`` on
every pod restart and stays empty until ``refresh_symbol_map`` (the
~10s TWSE fetch) completes — leaving chip-rendering, search, and
``GET /api/tw/industry/{symbol}`` blank in the meantime. The cache
sits between the upstream fetch and the accessors so a cold-start
pod (or a sibling whose TWSE call lost to rate-limiting) can still
serve names from the last good snapshot.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_in_memory_maps():
    """Clear the module-level company-info maps between tests so a
    leftover entry from one test doesn't leak into the next."""
    import services.tw_market_service as tw
    tw._exchange_map.clear()
    tw._industry_map.clear()
    tw._name_map.clear()
    yield
    tw._exchange_map.clear()
    tw._industry_map.clear()
    tw._name_map.clear()


@pytest.mark.asyncio
async def test_refresh_symbol_map_writes_snapshot_to_redis():
    import services.tw_market_service as tw

    twse_rows = [{"Code": "2330"}, {"Code": "2454"}]
    industry_rows = [
        {"symbol": "2330", "name_zh": "台積電", "industry": "半導體業"},
        {"symbol": "2454", "name_zh": "聯發科", "industry": "半導體業"},
    ]

    set_mock = AsyncMock()
    with patch.object(tw.twse, "get_all_twse_symbols",
                      AsyncMock(return_value=twse_rows)), \
         patch.object(tw.twse, "get_all_tpex_symbols",
                      AsyncMock(return_value=[])), \
         patch.object(tw.twse, "get_listed_company_industries",
                      AsyncMock(return_value=industry_rows)), \
         patch("cache.redis_cache.cache_set", set_mock):
        await tw.refresh_symbol_map()

    assert set_mock.await_count == 1
    key, payload, ttl = set_mock.await_args.args
    assert key == "tw:symbol_map:v1"
    assert ttl == 7 * 24 * 3600
    snapshot = json.loads(payload)
    assert snapshot["exchange"]["2330"] == "TWSE"
    assert snapshot["name"]["2330"] == "台積電"
    assert snapshot["industry"]["2454"] == "半導體業"


@pytest.mark.asyncio
async def test_refresh_symbol_map_tolerates_redis_write_failure():
    """A Redis outage during snapshot write must not break the
    upstream refresh — in-memory state is still updated; only the
    cache replication is best-effort."""
    import services.tw_market_service as tw

    with patch.object(tw.twse, "get_all_twse_symbols",
                      AsyncMock(return_value=[{"Code": "2330"}])), \
         patch.object(tw.twse, "get_all_tpex_symbols",
                      AsyncMock(return_value=[])), \
         patch.object(tw.twse, "get_listed_company_industries",
                      AsyncMock(return_value=[
                          {"symbol": "2330", "name_zh": "台積電",
                           "industry": "半導體業"},
                      ])), \
         patch("cache.redis_cache.cache_set",
               AsyncMock(side_effect=RuntimeError("redis down"))):
        await tw.refresh_symbol_map()

    assert tw.get_company_name("2330") == "台積電"
    assert tw.get_industry("2330") == "半導體業"


@pytest.mark.asyncio
async def test_load_symbol_map_from_cache_populates_in_memory_maps():
    """Cold-start path: in-memory maps start empty, Redis has a
    snapshot, accessors return names without ever hitting TWSE."""
    import services.tw_market_service as tw

    snapshot = json.dumps({
        "exchange": {"2330": "TWSE", "3037": "TWSE"},
        "industry": {"2330": "半導體業", "3037": "其他電子業"},
        "name": {"2330": "台積電", "3037": "欣興"},
    })
    with patch("cache.redis_cache.cache_get",
               AsyncMock(return_value=snapshot)):
        loaded = await tw.load_symbol_map_from_cache()

    assert loaded is True
    assert tw.get_company_name("2330") == "台積電"
    assert tw.get_company_name("3037") == "欣興"
    assert tw.get_industry("3037") == "其他電子業"
    assert tw.get_exchange("2330") == "TWSE"


@pytest.mark.asyncio
async def test_load_symbol_map_from_cache_missing_key_is_noop():
    """No cached snapshot yet → return False so the caller can fall
    through to the upstream refresh. In-memory maps stay empty,
    accessors return None / default — not a crash."""
    import services.tw_market_service as tw

    with patch("cache.redis_cache.cache_get", AsyncMock(return_value=None)):
        loaded = await tw.load_symbol_map_from_cache()

    assert loaded is False
    assert tw.get_company_name("2330") is None


@pytest.mark.asyncio
async def test_load_symbol_map_from_cache_handles_redis_outage():
    """Redis outage during warmup is non-fatal — the upstream
    refresh still runs and rebuilds the map from scratch."""
    import services.tw_market_service as tw

    with patch("cache.redis_cache.cache_get",
               AsyncMock(side_effect=RuntimeError("connection refused"))):
        loaded = await tw.load_symbol_map_from_cache()

    assert loaded is False


@pytest.mark.asyncio
async def test_load_symbol_map_from_cache_ignores_malformed_payload():
    """Corrupted JSON in Redis must not crash startup."""
    import services.tw_market_service as tw

    with patch("cache.redis_cache.cache_get",
               AsyncMock(return_value="not-json{{{")):
        loaded = await tw.load_symbol_map_from_cache()

    assert loaded is False
    assert tw.get_company_name("2330") is None
