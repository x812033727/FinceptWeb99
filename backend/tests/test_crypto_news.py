"""
Pure-unit tests for the crypto news pipeline. Mocks Redis + Google News
RSS so the test runs offline.
"""
from unittest.mock import AsyncMock, patch

import pytest

import services.crypto_market_service as svc


@pytest.mark.asyncio
async def test_get_news_uses_full_name_in_query():
    """`AVAX` becomes `AVAX Avalanche crypto` so Google News surfaces both
    coverage tagged with the symbol and coverage that uses the full name."""
    captured: dict = {}

    async def _spy(query: str, limit: int = 10):
        captured["query"] = query
        return []

    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc, "_google_news_rss", new=_spy):
        await svc.get_news("AVAX", limit=5)

    assert captured["query"] == "AVAX Avalanche crypto"


@pytest.mark.asyncio
async def test_get_news_unknown_symbol_uses_cryptocurrency_keyword():
    """Symbols not in the curated NAMES map get a generic crypto query."""
    captured: dict = {}

    async def _spy(query: str, limit: int = 10):
        captured["query"] = query
        return []

    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc, "_google_news_rss", new=_spy):
        await svc.get_news("ZZZZ", limit=5)

    assert captured["query"] == "ZZZZ cryptocurrency"


@pytest.mark.asyncio
async def test_get_news_returns_parsed_items():
    items = [
        {"title": "Avalanche network upgrade", "publisher": "CoinDesk",
         "link": "https://news.google.com/articles/x",
         "published_at": "2026-04-26T10:00:00+00:00", "thumbnail": None},
    ]
    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set", new_callable=AsyncMock) as cache_set, \
         patch.object(svc, "_google_news_rss", new_callable=AsyncMock, return_value=items):
        result = await svc.get_news("AVAX", limit=5)

    assert result == items
    cache_set.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_news_swallows_rss_failure():
    """RSS request blowing up returns [] instead of bubbling — empty page
    is better than a 502 to the user."""
    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc, "_google_news_rss", new_callable=AsyncMock,
                      side_effect=RuntimeError("rss down")):
        result = await svc.get_news("BTC", limit=5)

    assert result == []


@pytest.mark.asyncio
async def test_google_news_rss_parses_xml():
    sample = """<?xml version="1.0"?>
    <rss version="2.0">
      <channel>
        <item>
          <title>Bitcoin hits new high</title>
          <link>https://news.google.com/articles/x</link>
          <pubDate>Sat, 26 Apr 2026 10:00:00 GMT</pubDate>
          <source url="https://coindesk.com">CoinDesk</source>
        </item>
      </channel>
    </rss>"""

    class _MockResponse:
        text = sample
        def raise_for_status(self): return None

    class _MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def get(self, *_, **__): return _MockResponse()

    with patch("httpx.AsyncClient", return_value=_MockClient()):
        items = await svc._google_news_rss("BTC Bitcoin crypto", limit=10)

    assert len(items) == 1
    assert items[0]["title"] == "Bitcoin hits new high"
    assert items[0]["publisher"] == "CoinDesk"


def test_names_map_covers_top20():
    """Every symbol in TOP20 has a display name."""
    from data.crypto.symbols import TOP20, NAMES
    missing = [s for s in TOP20 if s not in NAMES]
    assert missing == [], f"missing names for {missing}"
