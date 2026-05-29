"""Tests for the new DB read tier in tw_market_service.get_quote.

Verifies the upstream-fail → DB snapshot path. The repository reader
is mocked so the test stays at the service-flow layer.
"""
from unittest.mock import AsyncMock, patch

import pytest

import services.tw_market_service as tw


@pytest.fixture
def patch_io():
    with patch.object(tw, "cache_get_json", AsyncMock(return_value=None)) as cache_get, \
         patch.object(tw, "cache_set_json", AsyncMock()) as cache_set, \
         patch.object(tw, "fetch_quote_waterfall", AsyncMock(return_value=(None, "unavailable"))) as upstream, \
         patch("services.ingest.repository.read_latest_quote_autosession",
               AsyncMock(return_value=None)) as db_read:
        yield {
            "cache_get_json": cache_get, "cache_set_json": cache_set,
            "upstream": upstream, "db_read": db_read,
        }


@pytest.mark.asyncio
async def test_upstream_fail_db_hit_serves_snapshot(patch_io):
    """When TWSE+FinMind are both blocked, a recent DB snapshot fills in."""
    patch_io["db_read"].return_value = {
        "symbol": "2330", "market": "TW",
        "price": 605.0, "change_pct": 0.83, "prev_close": 600.0,
        "volume": 1_000, "ts": 1714283400000, "data_source": "twse",
    }

    out = await tw.get_quote("2330")

    assert out["price"] == 605.0
    assert out["data_source"] == "twse"
    patch_io["db_read"].assert_awaited_once()


@pytest.mark.asyncio
async def test_upstream_fail_db_miss_returns_blank(patch_io):
    """Both upstream and DB miss → caller sees the existing blank state
    (data_source='unavailable') and doesn't get cached."""
    out = await tw.get_quote("ZZZZ")
    assert out["data_source"] == "unavailable"
    assert out["price"] == 0
    patch_io["cache_set_json"].assert_not_awaited()


@pytest.mark.asyncio
async def test_upstream_success_skips_db_read(patch_io):
    """Happy path: upstream returns a quote, DB read isn't even attempted."""
    patch_io["upstream"].return_value = (
        {"close": 605.0, "volume": 1000, "prev_close": 600.0}, "twse",
    )

    out = await tw.get_quote("2330")

    assert out["price"] == 605.0
    assert out["data_source"] == "twse"
    patch_io["db_read"].assert_not_awaited()
    patch_io["cache_set_json"].assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_hit_skips_upstream_and_db(patch_io):
    cached = {"symbol": "2330", "market": "TW", "price": 600.0, "data_source": "twse"}
    patch_io["cache_get_json"].return_value = cached

    out = await tw.get_quote("2330")

    assert out["price"] == 600.0
    patch_io["upstream"].assert_not_awaited()
    patch_io["db_read"].assert_not_awaited()
