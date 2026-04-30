"""Tests for data.tw.google_news_tw_connector — the TW Google News RSS reader.

Mocks `httpx.AsyncClient` so tests run offline. Hand-builds RSS XML
fixtures mirroring real Google News responses (tested against the
production endpoint while writing).
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from data.tw import google_news_tw_connector as conn


def _rss(items: list[dict]) -> str:
    """Build a Google News-shaped RSS XML doc from a list of item dicts.

    Each dict can carry: title, link, pubDate, description, source.
    Missing keys → element omitted entirely (matches Google News'
    actual behaviour for some entries).
    """
    parts = ["<?xml version='1.0' encoding='UTF-8'?>",
             "<rss version='2.0'><channel>"]
    for it in items:
        parts.append("<item>")
        if "title" in it:
            parts.append(f"<title>{it['title']}</title>")
        if "link" in it:
            parts.append(f"<link>{it['link']}</link>")
        if "pubDate" in it:
            parts.append(f"<pubDate>{it['pubDate']}</pubDate>")
        if "description" in it:
            parts.append(f"<description>{it['description']}</description>")
        if "source" in it:
            parts.append(f"<source url='https://example.com/'>{it['source']}</source>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "".join(parts)


def _patched_client(xml_text: str, *, status: int = 200):
    """Return a MagicMock AsyncClient that yields a single successful
    GET → response with `xml_text` body."""
    response = MagicMock()
    response.status_code = status
    response.text = xml_text
    response.raise_for_status = MagicMock()
    if status >= 400:
        request = httpx.Request("GET", conn._RSS_URL)
        # raise_for_status should fire when called
        actual_response = httpx.Response(status_code=status, request=request)
        response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                f"HTTP {status}", request=request, response=actual_response,
            ),
        )

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.get = AsyncMock(return_value=response)
    return client


# ── extract_symbol unit tests ─────────────────────────────────────


def test_extract_symbol_picks_first_4_digit_code():
    assert conn._extract_symbol("台積電(2330)Q1財報優於預期") == "2330"


def test_extract_symbol_handles_etf_5_digit():
    assert conn._extract_symbol("00878 高股息 ETF 配息公告") == "00878"


def test_extract_symbol_returns_none_when_no_code():
    assert conn._extract_symbol("台股大盤量縮整理") is None


def test_extract_symbol_first_match_wins():
    """Headlines often mention a primary subject early and incidentally
    cite secondary codes — first match should win."""
    title = "2330 台積電法說會帶動 2454 聯發科同步走高"
    assert conn._extract_symbol(title) == "2330"


def test_extract_symbol_skips_long_numbers():
    """An 8-digit date sequence shouldn't masquerade as a stock code."""
    assert conn._extract_symbol("20240115 大盤收盤資訊") is None


# ── split_title_publisher ─────────────────────────────────────────


def test_split_title_publisher_extracts_suffix():
    title, pub = conn._split_title_publisher("台積電大漲 - 經濟日報")
    assert title == "台積電大漲"
    assert pub == "經濟日報"


def test_split_title_publisher_no_suffix():
    title, pub = conn._split_title_publisher("台股盤後 - 加權收紅 - 鉅亨網")
    # The regex intentionally only strips the last "- ..." segment so a
    # multi-dash headline keeps its meaningful body intact.
    assert title == "台股盤後 - 加權收紅"
    assert pub == "鉅亨網"


def test_split_title_publisher_empty_returns_originals():
    title, pub = conn._split_title_publisher("無分隔線標題")
    assert title == "無分隔線標題"
    assert pub is None


# ── get_news happy path ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_news_parses_market_wide_and_per_symbol():
    xml_text = _rss([
        {
            "title": "台股盤後 - 加權收漲 - 鉅亨網",
            "link":  "https://news.google.com/articles/AAA",
            "pubDate": "Wed, 30 Apr 2026 02:00:00 GMT",
            "description": "今日加權指數上漲 1.2%",
            "source": "鉅亨網",
        },
        {
            "title": "台積電(2330)法說優於預期 - 經濟日報",
            "link":  "https://news.google.com/articles/BBB",
            "pubDate": "Wed, 30 Apr 2026 04:30:00 GMT",
            "source": "經濟日報",
        },
    ])

    with patch.object(conn.httpx, "AsyncClient",
                      return_value=_patched_client(xml_text)):
        items = await conn.get_news(limit=10)

    assert len(items) == 2

    market_wide = items[0]
    assert market_wide["title"] == "台股盤後 - 加權收漲"
    assert market_wide["link"] == "https://news.google.com/articles/AAA"
    assert market_wide["source_name"] == "鉅亨網"
    assert market_wide["symbol"] is None

    per_symbol = items[1]
    assert per_symbol["title"] == "台積電(2330)法說優於預期"
    assert per_symbol["symbol"] == "2330"
    assert per_symbol["source_name"] == "經濟日報"

    # Published timestamp normalised to ISO 8601 UTC.
    iso = per_symbol["published_at"]
    assert iso.endswith("+00:00")
    parsed = datetime.fromisoformat(iso)
    assert parsed == datetime(2026, 4, 30, 4, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_get_news_drops_items_missing_title_or_link():
    xml_text = _rss([
        {"link": "https://example.com/no-title"},
        {"title": "no link headline"},
        {
            "title": "Valid headline",
            "link":  "https://example.com/ok",
            "pubDate": "Wed, 30 Apr 2026 02:00:00 GMT",
        },
    ])

    with patch.object(conn.httpx, "AsyncClient",
                      return_value=_patched_client(xml_text)):
        items = await conn.get_news(limit=10)

    assert len(items) == 1
    assert items[0]["title"] == "Valid headline"


@pytest.mark.asyncio
async def test_get_news_falls_back_to_title_suffix_when_source_missing():
    """Some Google News entries omit `<source>`; we recover the
    publisher from the trailing ` - 出處` in the title."""
    xml_text = _rss([
        {
            "title": "台積電大漲 - 工商時報",
            "link":  "https://example.com/x",
            "pubDate": "Wed, 30 Apr 2026 02:00:00 GMT",
            # no <source>
        },
    ])

    with patch.object(conn.httpx, "AsyncClient",
                      return_value=_patched_client(xml_text)):
        items = await conn.get_news(limit=10)

    assert items[0]["source_name"] == "工商時報"


@pytest.mark.asyncio
async def test_get_news_respects_limit():
    xml_text = _rss([
        {"title": f"Headline {i}",
         "link":  f"https://example.com/{i}",
         "pubDate": "Wed, 30 Apr 2026 02:00:00 GMT"}
        for i in range(20)
    ])

    with patch.object(conn.httpx, "AsyncClient",
                      return_value=_patched_client(xml_text)):
        items = await conn.get_news(limit=5)

    assert len(items) == 5


@pytest.mark.asyncio
async def test_get_news_returns_empty_on_malformed_xml():
    """A parse error shouldn't blow up the ingest task — we log and
    return [], which the task treats as a healthy zero-row pass."""
    with patch.object(conn.httpx, "AsyncClient",
                      return_value=_patched_client("<not xml")):
        items = await conn.get_news(limit=10)

    assert items == []


@pytest.mark.asyncio
async def test_get_news_propagates_http_error():
    """5xx / 429 from Google News must propagate so the task records
    the failure and arms backoff. Silently swallowing would mask
    upstream outages."""
    with patch.object(conn.httpx, "AsyncClient",
                      return_value=_patched_client("", status=429)):
        with pytest.raises(httpx.HTTPStatusError):
            await conn.get_news(limit=10)


@pytest.mark.asyncio
async def test_get_news_default_query_used_when_unspecified():
    """Without an explicit `query`, the connector hits the canonical
    market-wide search string. Pinning this prevents an accidental
    refactor from silently shifting coverage."""
    captured: dict = {}
    response = MagicMock()
    response.status_code = 200
    response.text = _rss([])
    response.raise_for_status = MagicMock()

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    async def _fake_get(url, params=None):
        captured["url"] = url
        captured["params"] = params
        return response
    client.get = _fake_get

    with patch.object(conn.httpx, "AsyncClient", return_value=client):
        await conn.get_news()

    assert captured["url"] == conn._RSS_URL
    assert captured["params"]["q"] == conn.DEFAULT_QUERY
    assert captured["params"]["hl"] == "zh-TW"
    assert captured["params"]["gl"] == "TW"
    assert captured["params"]["ceid"] == "TW:zh-Hant"
