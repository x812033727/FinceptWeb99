"""Pure unit tests for the TW market service's don't-cache-empty behavior.

When TWSE + FinMind + MOPS all fail, callers used to lock in a TTL_QUOTE
(60s) of zero-state data. Mirroring `us_market_service.get_quote` we now
skip cache_set when the result is empty so the next request retries.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services import tw_market_service as svc


# ── get_quote ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_quote_zero_price_not_cached():
    """All upstream sources failed ⇒ price=0 ⇒ don't cache."""
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.twse, "get_realtime_quote", AsyncMock(side_effect=RuntimeError)), \
         patch.object(svc.finmind, "get_daily_ohlcv", AsyncMock(return_value=[])):
        result = await svc.get_quote("2330")

    mock_set.assert_not_awaited()
    assert result["price"] == 0


@pytest.mark.asyncio
async def test_get_quote_real_price_is_cached():
    raw = {
        "symbol": "2330", "name_zh": "台積電",
        "close": 820.0, "prev_close": 815.0, "change": 5.0,
        "open": 818.0, "high": 825.0, "low": 815.0, "volume": 25_000_000,
    }
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.twse, "get_realtime_quote", AsyncMock(return_value=raw)):
        result = await svc.get_quote("2330")

    mock_set.assert_awaited_once()
    assert result["price"] == 820.0


# ── get_history ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_history_empty_not_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.twse, "get_daily_ohlcv", AsyncMock(side_effect=RuntimeError)), \
         patch.object(svc.finmind, "get_daily_ohlcv", AsyncMock(return_value=[])):
        result = await svc.get_history("2330", months=1)

    mock_set.assert_not_awaited()
    assert result == []


# ── get_institutional ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_institutional_empty_not_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.finmind, "get_institutional", AsyncMock(return_value=[])), \
         patch.object(svc.twse, "get_institutional", AsyncMock(return_value=[])):
        result = await svc.get_institutional("2330")

    mock_set.assert_not_awaited()
    assert result == []


# ── get_margin ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_margin_empty_not_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.finmind, "get_margin", AsyncMock(return_value=[])), \
         patch.object(svc.twse, "get_margin", AsyncMock(return_value=[])):
        result = await svc.get_margin("2330")

    mock_set.assert_not_awaited()
    assert result == []


# ── get_revenue ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_revenue_empty_not_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.finmind, "get_monthly_revenue", AsyncMock(return_value=[])), \
         patch.object(svc.mops, "get_monthly_revenue_recent", AsyncMock(return_value=[])):
        result = await svc.get_revenue("2330")

    mock_set.assert_not_awaited()
    assert result == []


# ── backtest mode (as_of) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_institutional_as_of_skips_cache_and_live_tiers():
    """Backtest mode bypasses Redis (per-as_of cache key would explode)
    and skips live FinMind / TWSE fallbacks (would return today's data,
    leaking future information into the historical replay). Returns
    whatever the DB archive holds — possibly []."""
    from datetime import date
    from unittest.mock import MagicMock

    finmind_mock = AsyncMock(return_value=[{"foreign_buy": 9999}])
    twse_mock = AsyncMock(return_value=[{"foreign_buy": 9999}])
    cache_get_mock = AsyncMock(return_value='[{"cached":"row"}]')
    db_rows = [{"date": "2026-04-15", "foreign_buy": 1000}]

    # Patch the lazy import inside the function: services.ingest.repository.read_institutional_range
    with patch.object(svc, "cache_get", cache_get_mock), \
         patch.object(svc, "cache_set", AsyncMock()) as cache_set_mock, \
         patch.object(svc.finmind, "get_institutional", finmind_mock), \
         patch.object(svc.twse, "get_institutional", twse_mock), \
         patch("services.ingest.repository.read_institutional_range",
               new=AsyncMock(return_value=db_rows)), \
         patch("db.session.AsyncSessionLocal", MagicMock()):
        result = await svc.get_institutional(
            "2330", days=30, as_of=date(2026, 4, 15),
        )

    # Cache GET / SET both skipped in backtest mode.
    cache_get_mock.assert_not_awaited()
    cache_set_mock.assert_not_awaited()
    # Live tiers must NOT have been called — would have returned 9999.
    finmind_mock.assert_not_awaited()
    twse_mock.assert_not_awaited()
    assert result == db_rows


@pytest.mark.asyncio
async def test_get_institutional_as_of_returns_empty_on_db_miss():
    """Backtest mode + DB has no rows in the historical window → []
    (NOT a live fallback). Caller already treats blank as 'no signal'."""
    from datetime import date
    from unittest.mock import MagicMock

    finmind_mock = AsyncMock(return_value=[{"foreign_buy": 9999}])
    twse_mock = AsyncMock(return_value=[{"foreign_buy": 9999}])

    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc.finmind, "get_institutional", finmind_mock), \
         patch.object(svc.twse, "get_institutional", twse_mock), \
         patch("services.ingest.repository.read_institutional_range",
               new=AsyncMock(return_value=[])), \
         patch("db.session.AsyncSessionLocal", MagicMock()):
        result = await svc.get_institutional(
            "2330", days=30, as_of=date(2020, 1, 1),
        )

    assert result == []
    finmind_mock.assert_not_awaited()  # critical: no future-leak
    twse_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_margin_as_of_skips_cache_and_live_tiers():
    from datetime import date
    from unittest.mock import MagicMock

    finmind_mock = AsyncMock(return_value=[{"margin_purchase_balance": 9999}])
    cache_get_mock = AsyncMock(return_value='[{"cached":"row"}]')
    db_rows = [{"date": "2026-04-15", "margin_purchase_balance": 1000}]

    with patch.object(svc, "cache_get", cache_get_mock), \
         patch.object(svc, "cache_set", AsyncMock()) as cache_set_mock, \
         patch.object(svc.finmind, "get_margin", finmind_mock), \
         patch("services.ingest.repository.read_margin_range",
               new=AsyncMock(return_value=db_rows)), \
         patch("db.session.AsyncSessionLocal", MagicMock()):
        result = await svc.get_margin(
            "2330", days=30, as_of=date(2026, 4, 15),
        )

    cache_get_mock.assert_not_awaited()
    cache_set_mock.assert_not_awaited()
    finmind_mock.assert_not_awaited()
    assert result == db_rows


# ── get_fundamentals ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_fundamentals_no_ratios_not_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.twse, "get_valuation_ratios", AsyncMock(side_effect=RuntimeError)):
        result = await svc.get_fundamentals("2330")

    mock_set.assert_not_awaited()
    assert result["symbol"] == "2330"


@pytest.mark.asyncio
async def test_get_fundamentals_with_ratios_is_cached():
    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.twse, "get_valuation_ratios", AsyncMock(return_value={"pe": 18.5, "pb": 5.2})):
        result = await svc.get_fundamentals("2330")

    mock_set.assert_awaited_once()
    assert result["pe"] == 18.5


# ── find_symbol_by_name_in_text (PR #215) ─────────────────────────


def test_find_symbol_by_name_returns_none_when_map_empty():
    """Fresh deploy before symbol-map cron has run → graceful None,
    not exception."""
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        assert svc.find_symbol_by_name_in_text("台積電法說") is None
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)


def test_find_symbol_by_name_matches_company_name_in_title():
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        svc._name_map.update({
            "2330": "台積電",
            "2454": "聯發科",
            "2317": "鴻海",
        })
        assert svc.find_symbol_by_name_in_text(
            "台積電法說會超預期 - 經濟日報",
        ) == "2330"
        assert svc.find_symbol_by_name_in_text(
            "聯發科 Q1 EPS 公布",
        ) == "2454"
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)


def test_find_symbol_by_name_prefers_longer_match_on_collision():
    """`中華電` contains `中華` as a prefix. Prefix collision must
    resolve to the longer name, otherwise CHT (2412) headlines
    would mis-tag as some 中華-prefixed code."""
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        svc._name_map.update({
            "2412": "中華電",
            "1234": "中華",   # shorter — must NOT win when both could match
        })
        assert svc.find_symbol_by_name_in_text(
            "中華電宣布漲價 - 工商時報",
        ) == "2412"
        # Pure 中華 (no 電) should still match the shorter name.
        assert svc.find_symbol_by_name_in_text(
            "中華大樓開幕",
        ) == "1234"
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)


def test_find_symbol_by_name_returns_none_when_no_match():
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        svc._name_map.update({"2330": "台積電"})
        assert svc.find_symbol_by_name_in_text("純策略討論不提具體公司") is None
        assert svc.find_symbol_by_name_in_text("") is None
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)


# ── find_symbols_by_names_in_text (PR #221 multi-match) ─────────


def test_find_symbols_by_names_returns_multiple_matches():
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        svc._name_map.update({
            "2330": "台積電", "2454": "聯發科", "2317": "鴻海",
        })
        out = svc.find_symbols_by_names_in_text(
            "討論台積電 / 鴻海 短線", limit=5,
        )
        assert "2330" in out
        assert "2317" in out
        # 聯發科 not in text → not in result
        assert "2454" not in out
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)


def test_find_symbols_by_names_caps_at_limit():
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        svc._name_map.update({
            "2330": "台積電", "2454": "聯發科",
            "2317": "鴻海", "2412": "中華電",
        })
        out = svc.find_symbols_by_names_in_text(
            "台積電 聯發科 鴻海 中華電 都漲", limit=2,
        )
        assert len(out) == 2
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)


def test_find_symbols_by_names_masks_consumed_spans_for_prefix_collision():
    """中華電 masks its own occurrences before 中華 scans, so the
    shorter name only matches positions 中華電 didn't consume.
    "中華地產 vs 中華電" → 中華電 (consumes its slot) + 中華
    (still hits in 中華地產)."""
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        svc._name_map.update({
            "2412": "中華電",
            "1234": "中華",   # shorter, also matches
        })
        out = svc.find_symbols_by_names_in_text(
            "中華地產 vs 中華電", limit=5,
        )
        # Both should land — 中華電 first (longer), then 中華 from
        # the part of the text that isn't already masked.
        assert out == ["2412", "1234"]


        # Sanity: pure 中華 (no 中華電) still matches the shorter.
        out = svc.find_symbols_by_names_in_text("中華大樓開幕", limit=5)
        assert out == ["1234"]
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)


def test_find_symbols_by_names_returns_empty_when_no_matches():
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        svc._name_map.update({"2330": "台積電"})
        assert svc.find_symbols_by_names_in_text("純策略討論", limit=5) == []
        assert svc.find_symbols_by_names_in_text("", limit=5) == []
        assert svc.find_symbols_by_names_in_text("台積電", limit=0) == []
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)


def test_find_symbols_by_names_empty_when_map_empty():
    saved = dict(svc._name_map)
    try:
        svc._name_map.clear()
        assert svc.find_symbols_by_names_in_text("台積電法說", limit=5) == []
    finally:
        svc._name_map.clear()
        svc._name_map.update(saved)
