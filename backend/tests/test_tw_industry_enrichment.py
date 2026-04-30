"""Tests for the TW industry-tag enrichment.

Covers the in-memory `_industry_map` / `_name_map` plumbing:

  - `refresh_symbol_map` populates both from TWSE `t187ap03_L`
    plus the existing TPEx feed
  - `get_industry` / `get_company_name` accessors
  - `discussion_service._tag_industry` enriches rows that don't
    already carry the keys
  - GET /api/tw/industry/{symbol} surfaces the cached values

Connector calls are mocked so tests run offline.
"""
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


@pytest.fixture(autouse=True)
def reset_in_memory_maps():
    """Clear the module-level company-info maps between tests so a
    leftover entry from one test doesn't leak into the next."""
    import services.tw_market_service as tw
    tw._exchange_map.clear()
    tw._industry_map.clear()
    tw._name_map.clear()
    yield
    tw._exchange_map.clear()
    tw._industry_map.clear()
    tw._name_map.clear()


@pytest.mark.asyncio
async def test_refresh_symbol_map_populates_industry_and_name():
    import services.tw_market_service as tw

    twse_rows = [{"Code": "2330"}, {"Code": "2454"}]
    tpex_rows = []
    industry_rows = [
        {"symbol": "2330", "name_zh": "台積電", "industry": "半導體業"},
        {"symbol": "2454", "name_zh": "聯發科", "industry": "半導體業"},
        {"symbol": "9999", "name_zh": "新公司", "industry": None},
    ]

    with patch.object(tw.twse, "get_all_twse_symbols",
                      AsyncMock(return_value=twse_rows)), \
         patch.object(tw.twse, "get_all_tpex_symbols",
                      AsyncMock(return_value=tpex_rows)), \
         patch.object(tw.twse, "get_listed_company_industries",
                      AsyncMock(return_value=industry_rows)):
        await tw.refresh_symbol_map()

    assert tw.get_industry("2330") == "半導體業"
    assert tw.get_industry("2454") == "半導體業"
    # Symbol with NULL industry doesn't poison the map with empty
    # strings — accessor returns None.
    assert tw.get_industry("9999") is None
    assert tw.get_company_name("2330") == "台積電"


@pytest.mark.asyncio
async def test_refresh_tolerates_partial_upstream_failures():
    """t187ap03_L outage shouldn't break the exchange map. Industry
    map stays empty; exchange map still gets populated."""
    import services.tw_market_service as tw

    twse_rows = [{"Code": "2330"}]
    with patch.object(tw.twse, "get_all_twse_symbols",
                      AsyncMock(return_value=twse_rows)), \
         patch.object(tw.twse, "get_all_tpex_symbols",
                      AsyncMock(return_value=[])), \
         patch.object(tw.twse, "get_listed_company_industries",
                      AsyncMock(side_effect=RuntimeError("twse 503"))):
        await tw.refresh_symbol_map()

    assert tw.get_exchange("2330") == "TWSE"
    assert tw.get_industry("2330") is None


def test_tag_industry_enriches_rows():
    """`_tag_industry` adds industry + name_zh from the in-memory
    map for any row that lacks them. Rows that already carry the
    keys are passed through untouched."""
    import services.tw_market_service as tw
    from services import discussion_service

    tw._industry_map["2330"] = "半導體業"
    tw._name_map["2330"] = "台積電"

    rows = [
        {"symbol": "2330", "net_foreign_buy": 1_000_000},
        # Pre-tagged → don't clobber.
        {"symbol": "2454", "net_foreign_buy": 500_000,
         "industry": "preexisting", "name_zh": "preexisting"},
        # Unknown symbol → leave empty, no crash.
        {"symbol": "9999", "net_foreign_buy": 100_000},
    ]
    enriched = discussion_service._tag_industry(rows)

    assert enriched[0]["industry"] == "半導體業"
    assert enriched[0]["name_zh"] == "台積電"
    assert enriched[1]["industry"] == "preexisting"
    assert enriched[1]["name_zh"] == "preexisting"
    assert "industry" not in enriched[2] or enriched[2]["industry"] is None


# ── API endpoint ──────────────────────────────────────────────────


async def _auth_headers(
    client: AsyncClient, email: str = "industry_user@example.com",
) -> dict:
    await client.post(
        "/api/auth/register", json={"email": email, "password": "Pass1234!"},
    )
    r = await client.post(
        "/api/auth/login", json={"email": email, "password": "Pass1234!"},
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_industry_endpoint_returns_cached_value(client: AsyncClient):
    import services.tw_market_service as tw
    tw._industry_map["2330"] = "半導體業"
    tw._name_map["2330"] = "台積電"

    h = await _auth_headers(client, "industry_endpoint@example.com")
    r = await client.get("/api/tw/industry/2330", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "2330"
    assert body["industry"] == "半導體業"
    assert body["name_zh"] == "台積電"


@pytest.mark.asyncio
async def test_industry_endpoint_returns_nulls_for_unknown(
    client: AsyncClient,
):
    """Endpoint is informational — empty fields rather than 404 so
    the frontend can fall back to '—' without error handling."""
    h = await _auth_headers(client, "industry_unknown@example.com")
    r = await client.get("/api/tw/industry/0000", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["industry"] is None
    assert body["name_zh"] is None
