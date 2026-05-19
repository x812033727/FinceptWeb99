"""Tests for services.discussion.symbol_names — display-name lookup
for the conclusion and scoreboard surfaces.

Stubs the TW in-memory name map + the crypto NAMES dict so the test
doesn't depend on a populated symbol-map cron.
"""
from __future__ import annotations

from unittest.mock import patch

from services.discussion.symbol_names import (
    build_symbol_names_map,
    enrich_conclusion_with_names,
    resolve_display_name,
)


def test_resolve_display_name_tw_uses_in_memory_map():
    with patch(
        "services.tw_market_service.get_company_name",
        side_effect=lambda sym: {"2330": "台積電"}.get(sym),
    ):
        assert resolve_display_name("TW", "2330") == "台積電"
        assert resolve_display_name("TW", "9999") is None


def test_resolve_display_name_crypto_uses_top20_dict():
    # NAMES is static — no patching needed
    assert resolve_display_name("CRYPTO", "BTC") == "Bitcoin"
    assert resolve_display_name("CRYPTO", "btc") == "Bitcoin"
    assert resolve_display_name("CRYPTO", "DOGE") == "Dogecoin"
    assert resolve_display_name("CRYPTO", "ZZZ") is None


def test_resolve_display_name_us_returns_none():
    # US deliberately not handled — no zero-cost cached lookup.
    assert resolve_display_name("US", "AAPL") is None
    assert resolve_display_name("GLOBAL", "AAPL") is None


def test_resolve_display_name_handles_empty_inputs():
    assert resolve_display_name("", "2330") is None
    assert resolve_display_name("TW", "") is None


def test_build_symbol_names_map_skips_misses_and_dedupes():
    with patch(
        "services.tw_market_service.get_company_name",
        side_effect=lambda sym: {"2330": "台積電", "2454": "聯發科"}.get(sym),
    ):
        result = build_symbol_names_map(
            "TW", ["2330", "9999", "2454", "2330", None, ""],
        )
    assert result == {"2330": "台積電", "2454": "聯發科"}


def test_enrich_conclusion_pulls_from_both_recommended_and_recommendations():
    with patch(
        "services.tw_market_service.get_company_name",
        side_effect=lambda sym: {"2330": "台積電", "2454": "聯發科"}.get(sym),
    ):
        conclusion = {
            "recommended_symbols": ["2330"],
            "recommendations": [
                {"symbol": "2330", "confidence": 0.8},
                {"symbol": "2454", "confidence": 0.6},
            ],
            "reasoning": "...",
            "risks": [],
            "time_horizon": "short_term",
            "consensus_score": 0.7,
        }
        enrich_conclusion_with_names("TW", conclusion)
    assert conclusion["symbol_names"] == {"2330": "台積電", "2454": "聯發科"}


def test_enrich_conclusion_us_market_emits_empty_map():
    conclusion = {
        "recommended_symbols": ["AAPL", "TSLA"],
        "reasoning": "",
        "risks": [],
        "time_horizon": "medium_term",
        "consensus_score": 0.5,
    }
    enrich_conclusion_with_names("US", conclusion)
    assert conclusion["symbol_names"] == {}


def test_enrich_conclusion_returns_non_dict_unchanged():
    assert enrich_conclusion_with_names("TW", None) is None
    assert enrich_conclusion_with_names("TW", []) == []  # type: ignore[arg-type]
