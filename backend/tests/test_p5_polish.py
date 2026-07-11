"""P5: ETag middleware + waterfall per-tier timeout budget."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from middleware.etag import ETagMiddleware


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ETagMiddleware)

    @app.get("/api/us/screener")
    async def screener():
        return {"rows": list(range(50))}

    @app.get("/api/auth/whoami")
    async def whoami():
        return {"user": "u1"}

    return app


@pytest.mark.asyncio
async def test_etag_set_on_allowlisted_get():
    async with AsyncClient(
        transport=ASGITransport(app=_make_app()), base_url="http://t"
    ) as client:
        resp = await client.get("/api/us/screener")

    assert resp.status_code == 200
    assert resp.headers.get("etag", "").startswith('W/"')


@pytest.mark.asyncio
async def test_etag_returns_304_on_match():
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as client:
        first = await client.get("/api/us/screener")
        etag = first.headers["etag"]
        second = await client.get(
            "/api/us/screener", headers={"If-None-Match": etag}
        )

    assert second.status_code == 304
    assert second.headers["etag"] == etag
    assert second.content == b""


@pytest.mark.asyncio
async def test_etag_skips_paths_off_allowlist():
    async with AsyncClient(
        transport=ASGITransport(app=_make_app()), base_url="http://t"
    ) as client:
        resp = await client.get("/api/auth/whoami")

    assert resp.status_code == 200
    assert "etag" not in resp.headers


@pytest.mark.asyncio
async def test_waterfall_tier_timeout_falls_through_to_next_tier():
    """A hung primary must not stall the waterfall past the tier budget —
    the TimeoutError lands in the tier's except-handler and yfinance
    serves the quote."""
    import services.us_market_service as svc

    async def hang(_ticker):
        await asyncio.sleep(30)

    with patch.object(svc.settings, "WATERFALL_TIER_TIMEOUT_SECONDS", 0.05), \
         patch.object(svc, "_use_polygon", return_value=True), \
         patch.object(svc.polygon, "get_quote", side_effect=hang), \
         patch.object(svc.yfinance, "get_quote",
                      AsyncMock(return_value={"price": 123.0})):
        started = asyncio.get_event_loop().time()
        raw, source = await svc.fetch_quote_waterfall("AAPL")
        elapsed = asyncio.get_event_loop().time() - started

    assert source == "yfinance"
    assert raw["price"] == 123.0
    assert elapsed < 5          # nowhere near the 30s hang
