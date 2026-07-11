"""API tests for GET /api/{market}/intraday/{symbol} (A2 分時).

Covers auth enforcement, interval validation, the 200-with-empty-bars
contract, market scoping across the three routers, and a seeded TW
aggregation round-trip through the HTTP layer.
"""
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from services.ingest.repository import QuoteSnapshotRow, insert_quote_snapshot
from tasks.ingest_quotes_retention_tw import RETENTION_DAYS


async def _auth_headers(client: AsyncClient, email: str = "intraday@example.com") -> dict:
    await client.post("/api/auth/register", json={"email": email, "password": "Pass1234!"})
    r = await client.post("/api/auth/login", json={"email": email, "password": "Pass1234!"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _snap(symbol: str, ts: datetime, price: float, volume: int,
          market: str = "TW") -> QuoteSnapshotRow:
    return QuoteSnapshotRow(
        market=market, symbol=symbol, ts=ts,
        last_price=price, change_pct=0.1, prev_close=price - 1,
        volume=volume, source="twse",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/api/tw/intraday/2330",
    "/api/us/intraday/AAPL",
    "/api/crypto/intraday/BTC",
])
async def test_intraday_requires_auth(client: AsyncClient, path: str):
    r = await client.get(path)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_intraday_rejects_unknown_interval(client: AsyncClient):
    headers = await _auth_headers(client)
    r = await client.get("/api/tw/intraday/2330?interval=2m", headers=headers)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_intraday_empty_is_200_with_coverage(client: AsyncClient):
    """No snapshots for the symbol → 200 + empty bars + coverage note,
    never a 404/502 — 'no intraday data' is an expected state."""
    headers = await _auth_headers(client)
    r = await client.get("/api/tw/intraday/9999?interval=5m", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["bars"] == []
    assert body["coverage_days"] == RETENTION_DAYS
    assert body["interval"] == "5m"


@pytest.mark.asyncio
async def test_intraday_tw_aggregates_snapshots(
    client: AsyncClient, db_session: AsyncSession,
):
    headers = await _auth_headers(client)
    base = (datetime.now(UTC) - timedelta(days=1)).replace(
        hour=3, minute=0, second=0, microsecond=0,
    )
    # Two ticks in the first 5m bucket, one in the next.
    await insert_quote_snapshot(db_session, _snap("2330", base, 600.0, 1000))
    await insert_quote_snapshot(
        db_session, _snap("2330", base + timedelta(minutes=2), 603.0, 1600),
    )
    await insert_quote_snapshot(
        db_session, _snap("2330", base + timedelta(minutes=5), 601.0, 2100),
    )

    r = await client.get("/api/tw/intraday/2330?interval=5m", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "2330"
    assert body["market"] == "TW"
    assert len(body["bars"]) == 2
    b0, b1 = body["bars"]
    assert b0["open"] == 600.0 and b0["close"] == 603.0 and b0["high"] == 603.0
    assert b0["volume"] == 1600           # first bar of day: raw cumulative
    assert b1["volume"] == 500            # 2100 - 1600
    # Intraday `time` is Unix ms at the bucket boundary.
    assert b1["time"] - b0["time"] == 5 * 60 * 1000


@pytest.mark.asyncio
async def test_intraday_market_scoping_over_http(
    client: AsyncClient, db_session: AsyncSession,
):
    """A TW-market snapshot must not surface through the US router."""
    headers = await _auth_headers(client)
    await insert_quote_snapshot(
        db_session, _snap("TSM", datetime.now(UTC), 100.0, 500, market="TW"),
    )
    r_us = await client.get("/api/us/intraday/TSM?interval=1m", headers=headers)
    r_tw = await client.get("/api/tw/intraday/TSM?interval=1m", headers=headers)
    assert r_us.status_code == 200 and r_us.json()["bars"] == []
    assert r_tw.status_code == 200 and len(r_tw.json()["bars"]) == 1
