"""Tests for the new DB read tier in tw_market_service.get_fundamentals.

Verifies the Redis → DB(fresh) → upstream → DB(stale) ordering. Repository
helpers are mocked so the test stays focused on service flow.
"""
from unittest.mock import AsyncMock, patch

import pytest

import services.tw_market_service as tw


@pytest.fixture
def patch_io():
    with patch.object(tw, "cache_get_json", AsyncMock(return_value=None)) as cache_get, \
         patch.object(tw, "cache_set_json", AsyncMock()) as cache_set, \
         patch("services.ingest.repository.read_latest_fundamentals_autosession",
               AsyncMock(return_value=None)) as db_read, \
         patch("services.ingest.repository.upsert_fundamentals_snapshots_autosession",
               AsyncMock(return_value=0)) as db_write, \
         patch.object(tw.twse, "get_valuation_ratios",
                      AsyncMock(return_value={})) as upstream:
        yield {
            "cache_get_json": cache_get, "cache_set_json": cache_set,
            "db_read": db_read, "db_write": db_write, "upstream": upstream,
        }


@pytest.mark.asyncio
async def test_redis_hit_skips_db_and_upstream(patch_io):
    cached = {"symbol": "2330", "market": "TW", "pe_ratio": 15.0}
    patch_io["cache_get_json"].return_value = cached

    out = await tw.get_fundamentals("2330")

    assert out == cached
    patch_io["db_read"].assert_not_awaited()
    patch_io["upstream"].assert_not_awaited()


@pytest.mark.asyncio
async def test_db_fresh_hit_skips_upstream(patch_io):
    """A snapshot within 7 days returns directly without calling TWSE."""
    patch_io["db_read"].return_value = {
        "symbol": "2330", "market": "TW",
        "pe_ratio": 15.0, "pb_ratio": 2.5, "dividend_yield": 4.0,
        "as_of": "2026-04-27", "data_source": "twse",
    }

    out = await tw.get_fundamentals("2330")

    assert out["pe_ratio"] == 15.0
    assert out["data_source"] == "twse"
    assert out["fetched_at"] == "2026-04-27"
    patch_io["upstream"].assert_not_awaited()
    patch_io["cache_set_json"].assert_awaited_once()


@pytest.mark.asyncio
async def test_db_miss_falls_through_to_upstream(patch_io):
    """No fresh DB row → upstream called → result written back to DB."""
    patch_io["upstream"].return_value = {
        "pe_ratio": 18.0, "pb_ratio": 3.0, "dividend_yield": 3.5,
        "fetched_at": "2026-04-28",
    }

    out = await tw.get_fundamentals("2330")

    assert out["pe_ratio"] == 18.0
    patch_io["upstream"].assert_awaited()
    patch_io["db_write"].assert_awaited_once()
    patch_io["cache_set_json"].assert_awaited_once()


@pytest.mark.asyncio
async def test_upstream_failure_returns_stale_db(patch_io):
    """Upstream blank/fails AND no fresh DB → service queries with a wide
    window and returns whatever stale snapshot exists."""
    # First call (max_age_days=7) misses; second call (max_age_days=365) hits.
    patch_io["db_read"].side_effect = [
        None,
        {
            "symbol": "2330", "market": "TW",
            "pe_ratio": 12.0, "pb_ratio": 2.1, "dividend_yield": 5.0,
            "as_of": "2026-01-15", "data_source": "twse",
        },
    ]

    out = await tw.get_fundamentals("2330")

    assert out["pe_ratio"] == 12.0
    assert out["data_source"] == "db_stale"
    patch_io["db_write"].assert_not_awaited()


@pytest.mark.asyncio
async def test_total_blank_returns_base_only(patch_io):
    """Nothing anywhere → caller still gets the symbol/market/exchange shell."""
    out = await tw.get_fundamentals("UNKNOWN")
    assert out["symbol"] == "UNKNOWN"
    assert out["market"] == "TW"
    # No ratios populated.
    assert "pe_ratio" not in out or out.get("pe_ratio") is None
    patch_io["db_write"].assert_not_awaited()
    patch_io["cache_set_json"].assert_not_awaited()
