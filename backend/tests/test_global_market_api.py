"""Integration tests for /api/global/news endpoints."""
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


async def _auth_headers(
    client: AsyncClient, email: str = "global_user@example.com",
) -> dict:
    await client.post(
        "/api/auth/register", json={"email": email, "password": "Pass1234!"},
    )
    r = await client.post(
        "/api/auth/login", json={"email": email, "password": "Pass1234!"},
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_news_recent_reads_global_market_with_sentiment(
    client: AsyncClient,
):
    """Pin the call shape: market='GLOBAL', symbol IS NULL, sentiment
    on. Non-default values would silently drift the dashboard's
    international news card behaviour."""
    h = await _auth_headers(client, "global_recent@example.com")
    items = [
        {
            "title": "聯準會連三降息 美股收高",
            "publisher": "鉅亨網",
            "link": "https://example.com/intl/a",
            "published_at": "2026-04-30T08:00:00+00:00",
            "thumbnail": None,
            "data_source": "google_news_intl",
            "sentiment_score": 0.55,
            "sentiment_label": "bullish",
        },
    ]
    with patch(
        "services.ingest.repository.read_recent_news_autosession",
        new_callable=AsyncMock,
    ) as mock_read:
        mock_read.return_value = items
        r = await client.get("/api/global/news/recent?limit=20", headers=h)

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["sentiment_label"] == "bullish"
    args = mock_read.await_args
    # Positional `market` — first arg.
    assert args.args[0] == "GLOBAL"
    assert args.kwargs["symbol"] is None
    assert args.kwargs["include_sentiment"] is True


@pytest.mark.asyncio
async def test_news_recent_clamps_limit(client: AsyncClient):
    h = await _auth_headers(client, "global_clamp@example.com")
    r = await client.get("/api/global/news/recent?limit=999", headers=h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_news_recent_requires_auth(client: AsyncClient):
    """Same auth contract as the rest of the API — no anonymous reads."""
    r = await client.get("/api/global/news/recent")
    assert r.status_code in (401, 403)
