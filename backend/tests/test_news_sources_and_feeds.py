"""Tests for the news source registry + the direct-feed ingest task.

Pins: registry invariants (source key fits the DB column, market
filter), dictionary-first symbol tagging, row mapping, and the
"one dead feed is non-fatal / all feeds dead raises" contract.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import tasks.ingest_news_feeds as feeds
from services.news_sources import (
    SOURCES,
    enabled_sources,
    get_source,
)


# ── Registry ─────────────────────────────────────────────────────

def test_source_keys_fit_db_column_and_are_unique():
    keys = [s.key for s in SOURCES]
    assert len(keys) == len(set(keys)), "duplicate source keys"
    # news_articles.source is String(24).
    assert all(len(s.key) <= 24 for s in SOURCES)
    assert all(s.url.startswith("https://") for s in SOURCES)


def test_enabled_sources_filters_by_market():
    tw = enabled_sources(market="TW")
    assert tw and all(s.market == "TW" and s.enabled for s in tw)
    assert enabled_sources(market="NOPE") == []


def test_get_source_roundtrip():
    assert get_source("cnyes") is not None
    assert get_source("does_not_exist") is None


# ── Symbol tagging (dictionary-first) ────────────────────────────

def test_tag_symbol_prefers_name_dictionary_over_digits():
    # Name map resolves 台積電 → 2330; even though the title ALSO has a
    # digit code, the dictionary hit wins (it's the primary tagger).
    with patch(
        "services.tw_market_service.find_symbol_by_name_in_text",
        return_value="2330",
    ):
        assert feeds._tag_symbol("台積電法說會 5G 題材發酵") == "2330"


def test_tag_symbol_falls_back_to_digit_regex():
    with patch(
        "services.tw_market_service.find_symbol_by_name_in_text",
        return_value=None,
    ):
        assert feeds._tag_symbol("00878 高股息 ETF 除息") == "00878"
        assert feeds._tag_symbol("大盤收紅無個股") is None


def test_tag_symbol_survives_name_map_not_warm():
    # First boot: find_symbol_by_name_in_text raises → regex fallback.
    with patch(
        "services.tw_market_service.find_symbol_by_name_in_text",
        side_effect=RuntimeError("name map empty"),
    ):
        assert feeds._tag_symbol("2454 聯發科") == "2454"


# ── Row mapping ──────────────────────────────────────────────────

def test_to_row_maps_fields_and_source():
    with patch.object(feeds, "_tag_symbol", return_value="2330"):
        row = feeds._to_row(
            {
                "title": "台積電",
                "link": "https://x/a",
                "description": "  摘要  ",
                "published_at": "2026-07-12T01:02:03+00:00",
            },
            "cnyes",
        )
    assert row is not None
    assert row.source == "cnyes"
    assert row.symbol == "2330"
    assert row.summary == "摘要"
    assert row.market == "TW"


def test_to_row_drops_item_missing_link_or_date():
    assert feeds._to_row({"title": "x", "link": ""}, "cnyes") is None
    assert feeds._to_row(
        {"title": "x", "link": "https://x/a", "published_at": "bad"}, "cnyes"
    ) is None


# ── _do_run resilience ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_do_run_one_dead_feed_is_non_fatal(monkeypatch):
    """One feed raising is logged + skipped; the surviving feed's rows
    still get inserted. The job does NOT fail."""
    async def fake_fetch(url, *, limit):
        if "ltn" in url:
            raise RuntimeError("feed 503")
        return [{
            "title": "台積電",
            "link": f"{url}#a",
            "description": "",
            "published_at": "2026-07-12T01:02:03+00:00",
        }]

    inserted = {}

    async def fake_insert(db, rows):
        inserted["n"] = len(rows)
        return len(rows)

    monkeypatch.setattr(feeds, "fetch_feed", fake_fetch)
    monkeypatch.setattr(feeds, "insert_news_articles", fake_insert)
    monkeypatch.setattr(feeds, "_tag_symbol", lambda t: None)
    # Stub the session context manager.
    monkeypatch.setattr(feeds, "AsyncSessionLocal", _FakeSession)

    n = await feeds._do_run()
    # 4 sources, ltn dies → 3 rows.
    assert n == 3
    assert inserted["n"] == 3


@pytest.mark.asyncio
async def test_do_run_all_feeds_dead_raises(monkeypatch):
    async def fake_fetch(url, *, limit):
        raise RuntimeError("feed down")

    monkeypatch.setattr(feeds, "fetch_feed", fake_fetch)
    with pytest.raises(RuntimeError, match="all .* news feeds failed"):
        await feeds._do_run()


class _FakeSession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *a):
        return False
