"""Tests for the unified announcements API surface (PR-D4).

D1 + D3 wrote into `corporate_announcements` (TW MOPS + US SEC EDGAR);
D1b populated their sentiment fields. PR-D4 exposes the read tier
to the frontend via a single market-parameterized endpoint so the
dashboard's RecentAnnouncements card has one well-tested route to
consume.

Pinned invariants:
  - Endpoint requires auth (parity with the rest of /api/*).
  - Market is REQUIRED + restricted to {TW, US} — GLOBAL is NOT
    served because macro discussions don't bind to a single
    issuer-disclosure feed (matches the ctx block dispatch).
  - Limit + max_age_days are clamped via FastAPI Query bounds.
  - Symbol is optional and gets uppercased before the read tier
    sees it (so a frontend that sends 'aapl' doesn't silently miss
    rows that the connector wrote with 'AAPL').
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


async def _auth_headers(
    client: AsyncClient, email: str = "ann_user@example.com",
) -> dict:
    await client.post(
        "/api/auth/register",
        json={"email": email, "password": "Pass1234!"},
    )
    r = await client.post(
        "/api/auth/login",
        json={"email": email, "password": "Pass1234!"},
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_recent_requires_auth(client: AsyncClient):
    r = await client.get("/api/announcements/recent?market=TW")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_recent_requires_market_param(client: AsyncClient):
    h = await _auth_headers(client, "ann_market_required@example.com")
    r = await client.get("/api/announcements/recent", headers=h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_recent_rejects_unsupported_market(client: AsyncClient):
    """GLOBAL is intentionally not exposed — matches the ctx block's
    early-return for that market. Catching this at the endpoint layer
    keeps the contract honest at the API edge instead of inside the
    read tier."""
    h = await _auth_headers(client, "ann_global_rejected@example.com")
    r = await client.get(
        "/api/announcements/recent?market=GLOBAL", headers=h,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_recent_returns_tw_market_view(client: AsyncClient):
    h = await _auth_headers(client, "ann_tw_view@example.com")
    items = [
        {
            "symbol": "2330",
            "announced_at": "2026-04-30T06:00:00+00:00",
            "category": "財報",
            "title": "台積電 113 年第一季合併財報",
            "body": None,
            "source_url": "https://mops.example/2330",
            "sentiment_score": 0.6,
            "sentiment_label": "bullish",
        },
    ]
    with patch(
        "services.ingest.repository.read_recent_announcements_autosession",
        new_callable=AsyncMock,
    ) as mock_read:
        mock_read.return_value = items
        r = await client.get(
            "/api/announcements/recent?market=TW&limit=20", headers=h,
        )

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["symbol"] == "2330"
    assert body[0]["sentiment_label"] == "bullish"

    # Pin the call shape: market is the first positional, symbol
    # defaults to None for the cross-symbol view, sentiment fields
    # arrive from the read tier (no extra kwarg needed — it always
    # includes them).
    args, kwargs = mock_read.await_args
    assert args == ("TW",)
    assert kwargs["symbol"] is None
    assert kwargs["limit"] == 20


@pytest.mark.asyncio
async def test_recent_uppercases_symbol_filter(client: AsyncClient):
    """Frontend cards may send the symbol in either case; the
    connector writes uppercase. Normalize at the API edge so a
    `?symbol=aapl` query doesn't silently miss rows."""
    h = await _auth_headers(client, "ann_uppercase@example.com")
    with patch(
        "services.ingest.repository.read_recent_announcements_autosession",
        new_callable=AsyncMock,
    ) as mock_read:
        mock_read.return_value = []
        await client.get(
            "/api/announcements/recent?market=US&symbol=aapl", headers=h,
        )
    kwargs = mock_read.await_args.kwargs
    assert kwargs["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_recent_returns_us_view_with_sec_filings(client: AsyncClient):
    """Symmetry with TW: same endpoint, different market. The market
    filter on the underlying read tier keeps TW + US isolated even
    though they share a table."""
    h = await _auth_headers(client, "ann_us_view@example.com")
    items = [
        {
            "symbol": "AAPL",
            "announced_at": "2026-05-09T13:30:00+00:00",
            "category": "8-K",
            "title": "8-K - Apple Inc. (0000320193) (Filer)",
            "body": "Form 8-K material event",
            "source_url": "https://www.sec.gov/aapl/...",
            "sentiment_score": None,
            "sentiment_label": None,
        },
    ]
    with patch(
        "services.ingest.repository.read_recent_announcements_autosession",
        new_callable=AsyncMock,
    ) as mock_read:
        mock_read.return_value = items
        r = await client.get(
            "/api/announcements/recent?market=US&limit=10", headers=h,
        )

    assert r.status_code == 200
    body = r.json()
    assert body[0]["symbol"] == "AAPL"
    assert body[0]["category"] == "8-K"
    args, _ = mock_read.await_args
    assert args == ("US",)


@pytest.mark.asyncio
async def test_recent_clamps_limit_at_50(client: AsyncClient):
    h = await _auth_headers(client, "ann_limit_clamp@example.com")
    r = await client.get(
        "/api/announcements/recent?market=TW&limit=999", headers=h,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_recent_clamps_max_age_days(client: AsyncClient):
    h = await _auth_headers(client, "ann_age_clamp@example.com")
    r = await client.get(
        "/api/announcements/recent?market=TW&max_age_days=999",
        headers=h,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_recent_returns_502_on_repository_error(client: AsyncClient):
    """Read tier uncaught exception → 502 with a hint, not 500.
    Matches the rest of the market-data API surface."""
    h = await _auth_headers(client, "ann_502@example.com")
    with patch(
        "services.ingest.repository.read_recent_announcements_autosession",
        new_callable=AsyncMock,
        side_effect=RuntimeError("DB pool exhausted"),
    ):
        r = await client.get(
            "/api/announcements/recent?market=TW", headers=h,
        )
    assert r.status_code == 502
    detail = r.json().get("detail") or ""
    assert "archive read failed" in detail
