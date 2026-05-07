"""Unit tests for ai/tools/openai_compat.py — verify the new tools added
in PR #346 (`get_options_chain`, `get_symbol_news`, `get_symbol_sentiment`).

The OpenAI-compat path duplicates the Claude Agent toolset by design
(separate JSON schemas vs `@tool` decorators), so we exercise each tool's
dispatch handler independently from `test_ai_tools.py` to catch
regressions where one path is updated without the other.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from ai.tools.openai_compat import build_openai_compat_toolset


def _user_id() -> str:
    return str(uuid.uuid4())


def _payload(s: str) -> dict:
    return json.loads(s)


# ── schema surface ──────────────────────────────────────────────────


def test_toolset_exposes_new_tools_in_dispatch():
    """Adding to schemas without dispatch entries (or vice versa) is the
    classic schema/handler drift bug. Pin the contract."""
    schemas, dispatch = build_openai_compat_toolset(_user_id())
    schema_names = {s["function"]["name"] for s in schemas}
    expected = {
        "get_quote", "run_dcf", "run_var", "run_backtest", "query_user_data",
        "get_options_chain", "get_symbol_news", "get_symbol_sentiment",
    }
    assert expected <= schema_names
    assert expected <= set(dispatch.keys())


def test_options_chain_schema_requires_only_symbol():
    """`expiration` is optional — absence should produce a valid call
    that defaults to the nearest expiry, so the schema must NOT mark
    it required."""
    schemas, _ = build_openai_compat_toolset(_user_id())
    spec = next(s for s in schemas if s["function"]["name"] == "get_options_chain")
    params = spec["function"]["parameters"]
    assert params["required"] == ["symbol"]
    assert "expiration" in params["properties"]


# ── get_options_chain handler ───────────────────────────────────────


def _contract(strike: float, expiration: str, volume: int = 0, oi: int = 0) -> dict:
    return {
        "ticker": f"NVDA{expiration}{int(strike)}",
        "underlying_ticker": "NVDA",
        "contract_type": "call",
        "expiration_date": expiration,
        "strike_price": strike,
        "last_price": 1.0, "bid": 0.9, "ask": 1.1,
        "volume": volume, "open_interest": oi,
        "implied_volatility": 0.4,
        "in_the_money": False,
    }


@pytest.mark.asyncio
async def test_options_chain_focuses_nearest_when_unspecified():
    _, dispatch = build_openai_compat_toolset(_user_id())
    chain = [
        _contract(100, "2026-06-20", volume=10, oi=5),
        _contract(100, "2026-09-19", volume=999, oi=999),
    ]
    with patch("services.us_market_service.get_options",
               new_callable=AsyncMock) as mock:
        mock.return_value = chain
        result = _payload(await dispatch["get_options_chain"]({"symbol": "NVDA"}))

    assert result["expiration"] == "2026-06-20"
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_options_chain_caps_30_by_liquidity():
    _, dispatch = build_openai_compat_toolset(_user_id())
    chain = [
        _contract(strike=k, expiration="2026-06-20", volume=k, oi=k)
        for k in range(1, 51)
    ]
    with patch("services.us_market_service.get_options",
               new_callable=AsyncMock) as mock:
        mock.return_value = chain
        result = _payload(await dispatch["get_options_chain"]({
            "symbol": "NVDA", "expiration": "2026-06-20",
        }))

    assert result["count"] == 30
    strikes = {c["strike_price"] for c in result["contracts"]}
    assert 50 in strikes  # most liquid retained
    assert 1 not in strikes  # least liquid pruned


@pytest.mark.asyncio
async def test_options_chain_empty_returns_safe_payload():
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch("services.us_market_service.get_options",
               new_callable=AsyncMock) as mock:
        mock.return_value = []
        result = _payload(await dispatch["get_options_chain"]({"symbol": "DELISTED"}))
    assert result["count"] == 0
    assert result["contracts"] == []


@pytest.mark.asyncio
async def test_options_chain_service_failure_surfaces_error():
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch("services.us_market_service.get_options",
               new_callable=AsyncMock) as mock:
        mock.side_effect = RuntimeError("upstream timeout")
        result = _payload(await dispatch["get_options_chain"]({"symbol": "NVDA"}))
    assert "error" in result


# ── get_symbol_news handler ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_symbol_news_routes_us_and_caps_limit():
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch("services.us_market_service.get_news",
               new_callable=AsyncMock) as mock:
        mock.return_value = [{"title": "AAPL beats Q1", "link": "https://x"}]
        result = _payload(await dispatch["get_symbol_news"]({
            "symbol": "aapl", "market": "US", "limit": 999,
        }))
    mock.assert_awaited_once_with("AAPL", limit=20)
    assert result["market"] == "US"


@pytest.mark.asyncio
async def test_symbol_news_routes_tw_default_limit():
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch("services.tw_market_service.get_news",
               new_callable=AsyncMock) as mock:
        mock.return_value = []
        await dispatch["get_symbol_news"]({"symbol": "2330", "market": "tw"})
    mock.assert_awaited_once_with("2330", limit=10)


@pytest.mark.asyncio
async def test_symbol_news_rejects_unknown_market():
    _, dispatch = build_openai_compat_toolset(_user_id())
    result = _payload(await dispatch["get_symbol_news"]({
        "symbol": "X", "market": "JP",
    }))
    assert "error" in result


# ── get_symbol_sentiment handler ────────────────────────────────────


@pytest.mark.asyncio
async def test_symbol_sentiment_returns_aggregate():
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch(
        "services.news_sentiment_service.read_symbol_sentiment",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = {
            "symbol": "2330", "considered": 12, "count": 12,
            "avg_score": 0.42, "bullish": 8, "bearish": 1, "neutral": 3,
            "headlines": [],
        }
        result = _payload(await dispatch["get_symbol_sentiment"]({
            "symbol": "2330", "market": "TW",
        }))
    assert result["avg_score"] == 0.42
    assert result["bullish"] == 8


@pytest.mark.asyncio
async def test_symbol_sentiment_no_scored_articles_returns_zero():
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch(
        "services.news_sentiment_service.read_symbol_sentiment",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = None
        result = _payload(await dispatch["get_symbol_sentiment"]({
            "symbol": "OBSCURE", "market": "US",
        }))
    assert result["considered"] == 0
    assert result["headlines"] == []


@pytest.mark.asyncio
async def test_symbol_sentiment_max_age_clamped():
    """`max_age_hours` > 720 (30 days) silently clamped — protects
    against an LLM accidentally asking for the full sentiment history."""
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch(
        "services.news_sentiment_service.read_symbol_sentiment",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = None
        await dispatch["get_symbol_sentiment"]({
            "symbol": "X", "market": "US", "max_age_hours": 99999,
        })
    args = mock.await_args.kwargs
    assert args["max_age_hours"] == 720


@pytest.mark.asyncio
async def test_symbol_sentiment_rejects_unknown_market():
    _, dispatch = build_openai_compat_toolset(_user_id())
    result = _payload(await dispatch["get_symbol_sentiment"]({
        "symbol": "X", "market": "JP",
    }))
    assert "error" in result
