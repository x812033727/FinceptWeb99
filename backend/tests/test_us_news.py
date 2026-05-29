"""
Pure-unit tests for the US news pipeline. Mocks Redis + Google News RSS +
yfinance so the test runs offline.
"""
from unittest.mock import AsyncMock, patch

import pytest

import services.us_market_service as svc


@pytest.mark.asyncio
async def test_get_news_uses_google_when_yfinance_empty():
    google_items = [
        {"title": "Apple Q4 earnings", "publisher": "Reuters",
         "link": "https://news.google.com/articles/x",
         "published_at": "2026-04-26T10:00:00+00:00", "thumbnail": None},
    ]
    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc, "_google_news_rss", new_callable=AsyncMock, return_value=google_items) as g, \
         patch.object(svc, "_yfinance_news_fallback", new_callable=AsyncMock, return_value=[]) as yf:
        result = await svc.get_news("AAPL", limit=5)

    assert result == google_items
    g.assert_awaited_once()
    yf.assert_not_called()


@pytest.mark.asyncio
async def test_get_news_falls_back_to_yfinance_when_google_empty():
    yf_items = [{"title": "via yfinance", "publisher": "yf",
                 "link": "https://example.com", "published_at": "",
                 "thumbnail": None}]
    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc, "_google_news_rss", new_callable=AsyncMock, return_value=[]), \
         patch.object(svc, "_yfinance_news_fallback", new_callable=AsyncMock, return_value=yf_items):
        result = await svc.get_news("AAPL", limit=5)

    assert result == yf_items


@pytest.mark.asyncio
async def test_get_news_uses_cached_quote_name_in_query():
    """When quote cache has a name, it's appended to the search query."""
    quote_payload = {"name": "Apple Inc.", "price": 175.0}
    captured: dict = {}

    async def _spy(query: str, limit: int = 10):
        captured["query"] = query
        return []

    async def _cache_get(key):
        # cache hit on quote, miss on news
        return quote_payload if key.startswith("us:quote") or "quote" in key else None

    with patch.object(svc, "cache_get_json", new=_cache_get), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc, "_google_news_rss", new=_spy), \
         patch.object(svc, "_yfinance_news_fallback", new_callable=AsyncMock, return_value=[]):
        await svc.get_news("AAPL", limit=5)

    assert "AAPL" in captured["query"]
    assert "Apple Inc." in captured["query"]


@pytest.mark.asyncio
async def test_get_news_falls_back_to_fallback_universe_name():
    """No cached quote → company name pulled from the curated fallback list."""
    captured: dict = {}

    async def _spy(query: str, limit: int = 10):
        captured["query"] = query
        return []

    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc, "_google_news_rss", new=_spy), \
         patch.object(svc, "_yfinance_news_fallback", new_callable=AsyncMock, return_value=[]):
        await svc.get_news("NVDA", limit=5)

    # NVDA is in get_fallback_universe with name "NVIDIA Corporation"
    assert "NVDA" in captured["query"]
    assert "NVIDIA" in captured["query"]


@pytest.mark.asyncio
async def test_get_news_uses_stock_keyword_for_unknown_ticker():
    """Unknown ticker (not in fallback, no cache) falls through to `<sym> stock`."""
    captured: dict = {}

    async def _spy(query: str, limit: int = 10):
        captured["query"] = query
        return []

    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc, "_google_news_rss", new=_spy), \
         patch.object(svc, "_yfinance_news_fallback", new_callable=AsyncMock, return_value=[]):
        await svc.get_news("ZZZZ", limit=5)

    assert captured["query"] == "ZZZZ stock"


@pytest.mark.asyncio
async def test_google_news_rss_parses_xml():
    sample = """<?xml version="1.0"?>
    <rss version="2.0">
      <channel>
        <item>
          <title>Apple beats Q4</title>
          <link>https://news.google.com/articles/abc</link>
          <pubDate>Sat, 26 Apr 2026 10:00:00 GMT</pubDate>
          <source url="https://reuters.com">Reuters</source>
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
        items = await svc._google_news_rss("AAPL Apple Inc.", limit=10)

    assert len(items) == 1
    assert items[0]["title"] == "Apple beats Q4"
    assert items[0]["publisher"] == "Reuters"
    assert items[0]["link"].startswith("https://news.google.com/")
