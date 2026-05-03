"""Unit tests for `services.derivatives_service`.

Mocks the FinMind connector + Redis cache helpers so the suite stays
offline. Covers the parsing pivot (date × investor → net OI), trend
classification, cache hit / miss paths, and the four edge cases that
have bitten similar pivot helpers historically:
  - empty FinMind response
  - mixed Chinese / English investor names
  - dealer rows with sub-categories that need summing
  - missing OI fields (older FinMind shape)
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from services import derivatives_service as svc


def _row(d: str, name: str, long_oi: int, short_oi: int) -> dict:
    return {
        "date": d,
        "name": name,
        "long_open_interest_balance_volume": long_oi,
        "short_open_interest_balance_volume": short_oi,
    }


# ── _net_oi tolerance ────────────────────────────────────────────


def test_net_oi_uses_canonical_keys():
    row = {
        "long_open_interest_balance_volume": 5000,
        "short_open_interest_balance_volume": 2000,
    }
    assert svc._net_oi(row) == 3000


def test_net_oi_falls_back_to_alternate_field_names():
    """Older FinMind responses used `open_position_long_volume` /
    `open_position_short_volume`. Both shapes must work."""
    row = {
        "open_position_long_volume": 4000,
        "open_position_short_volume": 1500,
    }
    assert svc._net_oi(row) == 2500


def test_net_oi_returns_none_when_oi_fields_missing():
    """Row with only deal_volume (no OI) → None so caller skips
    instead of computing 0 net OI from missing data."""
    assert svc._net_oi({"long_deal_volume": 100}) is None


# ── _classify_trend ──────────────────────────────────────────────


def test_classify_trend_above_threshold_is_bullish():
    assert svc._classify_trend(1500) == "bullish"


def test_classify_trend_below_threshold_is_bearish():
    assert svc._classify_trend(-1500) == "bearish"


def test_classify_trend_within_band_is_neutral():
    assert svc._classify_trend(500) == "neutral"
    assert svc._classify_trend(-500) == "neutral"
    assert svc._classify_trend(0) == "neutral"


def test_classify_trend_none_input_is_neutral():
    """When 5d change can't be computed (only 1 session), call it
    neutral rather than crash."""
    assert svc._classify_trend(None) == "neutral"


# ── get_taifex_positioning end-to-end ────────────────────────────


@pytest.mark.asyncio
async def test_returns_none_when_finmind_empty():
    with patch.object(svc.cache_get, "__call__", new=AsyncMock(return_value=None)), \
         patch("services.derivatives_service.cache_get", new=AsyncMock(return_value=None)), \
         patch("services.derivatives_service.cache_set", new=AsyncMock()), \
         patch.object(svc.finmind, "get_futures_institutional",
                      new=AsyncMock(return_value=[])):
        result = await svc.get_taifex_positioning(
            contract="TX", as_of=date(2026, 4, 30),
        )
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_only_one_session():
    """Need ≥ 2 sessions to compute a 5-day change. One session in,
    return None rather than emit `change_5d=0` (which would falsely
    register as 'no positioning shift')."""
    rows = [_row("2026-04-30", "外資", 50_000, 30_000)]
    with patch("services.derivatives_service.cache_get", new=AsyncMock(return_value=None)), \
         patch("services.derivatives_service.cache_set", new=AsyncMock()), \
         patch.object(svc.finmind, "get_futures_institutional",
                      new=AsyncMock(return_value=rows)):
        result = await svc.get_taifex_positioning(
            contract="TX", as_of=date(2026, 4, 30),
        )
    assert result is None


@pytest.mark.asyncio
async def test_pivots_chinese_investor_names_into_canonical_groups():
    """FinMind rows arrive with Chinese investor labels. Pivot must
    map 外資 → fini, 投信 → sitc, 自營商 → dealer, then compute net OI
    per group + 5-day change (latest minus earliest in the window)."""
    rows = [
        # 5 days ago
        _row("2026-04-25", "外資", 40_000, 50_000),    # net = -10000
        _row("2026-04-25", "投信", 1_000, 500),          # net = +500
        _row("2026-04-25", "自營商", 2_000, 1_500),      # net = +500
        # latest
        _row("2026-04-30", "外資", 35_000, 60_000),    # net = -25000
        _row("2026-04-30", "投信", 1_500, 400),          # net = +1100
        _row("2026-04-30", "自營商", 1_800, 1_900),      # net = -100
    ]
    with patch("services.derivatives_service.cache_get", new=AsyncMock(return_value=None)), \
         patch("services.derivatives_service.cache_set", new=AsyncMock()), \
         patch.object(svc.finmind, "get_futures_institutional",
                      new=AsyncMock(return_value=rows)):
        result = await svc.get_taifex_positioning(
            contract="TX", as_of=date(2026, 4, 30),
        )

    assert result is not None
    assert result["contract"] == "TX"
    assert result["session_count"] == 2
    assert result["fini"]["net_oi"] == -25_000
    assert result["fini"]["change_5d"] == -15_000      # -25k - (-10k)
    assert result["sitc"]["net_oi"] == 1_100
    assert result["sitc"]["change_5d"] == 600          # 1100 - 500
    assert result["dealer"]["net_oi"] == -100
    # 外資 5d change = -15000 → bearish
    assert result["trend"] == "bearish"


@pytest.mark.asyncio
async def test_sums_dealer_subcategories_into_one_group():
    """FinMind splits 自營商 into 自行買賣 + 避險 (sometimes also
    Dealer_Self / Dealer_Hedge in English). All sub-rows on the same
    date must sum into a single dealer total per date."""
    rows = [
        _row("2026-04-25", "自營商(自行買賣)", 1_000, 800),
        _row("2026-04-25", "自營商(避險)",     2_000, 1_500),
        _row("2026-04-30", "自營商(自行買賣)", 1_500, 700),
        _row("2026-04-30", "自營商(避險)",     2_500, 1_200),
        # also need 外資 to satisfy >= 2 sessions
        _row("2026-04-25", "外資", 100, 100),
        _row("2026-04-30", "外資", 100, 100),
    ]
    with patch("services.derivatives_service.cache_get", new=AsyncMock(return_value=None)), \
         patch("services.derivatives_service.cache_set", new=AsyncMock()), \
         patch.object(svc.finmind, "get_futures_institutional",
                      new=AsyncMock(return_value=rows)):
        result = await svc.get_taifex_positioning(
            contract="TX", as_of=date(2026, 4, 30),
        )
    assert result is not None
    # 2026-04-30 dealer net = (1500-700) + (2500-1200) = 800 + 1300 = 2100
    assert result["dealer"]["net_oi"] == 2100
    # 2026-04-25 dealer net = (1000-800) + (2000-1500) = 200 + 500 = 700
    # 5d change = 2100 - 700 = 1400
    assert result["dealer"]["change_5d"] == 1400


@pytest.mark.asyncio
async def test_cache_hit_short_circuits_finmind_call():
    """When the Redis cache holds a JSON-serialised result for
    `(contract, as_of)`, FinMind must NOT be re-fetched. Cache TTL
    is the single throttle on quota burn."""
    cached_payload = {
        "contract": "TX", "as_of": "2026-04-30", "session_count": 5,
        "fini": {"net_oi": -20_000, "change_5d": -1_500},
        "sitc": {"net_oi": 800, "change_5d": 200},
        "dealer": {"net_oi": -50, "change_5d": 30},
        "trend": "bearish",
    }
    finmind_mock = AsyncMock()
    with patch(
        "services.derivatives_service.cache_get",
        new=AsyncMock(return_value=json.dumps(cached_payload)),
    ), patch.object(
        svc.finmind, "get_futures_institutional", new=finmind_mock,
    ):
        result = await svc.get_taifex_positioning(
            contract="TX", as_of=date(2026, 4, 30),
        )
    assert result == cached_payload
    finmind_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_miss_writes_result_back_with_ttl():
    """Cache miss → fetch from FinMind → write back to cache so the
    next call within the TTL window short-circuits."""
    rows = [
        _row("2026-04-25", "外資", 50_000, 40_000),
        _row("2026-04-30", "外資", 50_000, 40_000),
    ]
    cache_set_mock = AsyncMock()
    with patch("services.derivatives_service.cache_get", new=AsyncMock(return_value=None)), \
         patch("services.derivatives_service.cache_set", new=cache_set_mock), \
         patch.object(svc.finmind, "get_futures_institutional",
                      new=AsyncMock(return_value=rows)):
        result = await svc.get_taifex_positioning(
            contract="TX", as_of=date(2026, 4, 30),
        )
    assert result is not None
    cache_set_mock.assert_awaited_once()
    args, _ = cache_set_mock.call_args
    assert args[0] == "taifex:positioning:TX:2026-04-30"
    # TTL is the third positional arg
    assert args[2] == svc._CACHE_TTL_SECONDS
