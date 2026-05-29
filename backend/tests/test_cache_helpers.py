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

from cache import redis_cache as rc


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
