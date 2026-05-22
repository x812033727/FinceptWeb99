"""Tests for the DB-backed TW symbol-map persistence layer.

When the Redis snapshot is empty (first deploy of the
``tw:symbol_map:v1`` writer, or after a cache flush) and TWSE is
blocking the cloud-IP range — the failure mode that motivated the
deploy-survival layer in the first place — ``tw_company_info``
becomes the only path that lets a cold-start pod resolve 公司簡稱 /
產業 / 上市櫃 without sitting blank.

The cache fixture in ``conftest.py`` returns ``None`` from
``cache_get`` and discards ``cache_set``, so the Redis tier never
populates the in-memory maps during these tests — every successful
load below must come from the DB.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select


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


async def _seed_rows(rows: list[dict]) -> None:
    from db.session import AsyncSessionLocal
    from models.tw_company_info import TwCompanyInfo

    async with AsyncSessionLocal() as db:
        for r in rows:
            db.add(TwCompanyInfo(**r))
        await db.commit()


@pytest.mark.asyncio
async def test_load_symbol_map_from_db_populates_in_memory_maps():
    """Three rows in `tw_company_info` → all three in-memory maps
    pick them up; accessors return seeded values without a TWSE
    fetch."""
    import services.tw_market_service as tw

    await _seed_rows([
        {"symbol": "2330", "exchange": "TWSE",
         "industry": "半導體業", "name_zh": "台積電"},
        {"symbol": "2454", "exchange": "TWSE",
         "industry": "半導體業", "name_zh": "聯發科"},
        {"symbol": "6488", "exchange": "TPEx",
         "industry": "半導體業", "name_zh": "環球晶"},
    ])

    loaded = await tw.load_symbol_map_from_db()

    assert loaded is True
    assert tw.get_company_name("2330") == "台積電"
    assert tw.get_company_name("2454") == "聯發科"
    assert tw.get_company_name("6488") == "環球晶"
    assert tw.get_industry("2330") == "半導體業"
    assert tw.get_exchange("6488") == "TPEx"


@pytest.mark.asyncio
async def test_load_symbol_map_from_db_empty_returns_false():
    """Fresh DB, no rows → return False so the caller falls through
    to the upstream refresh. In-memory maps stay empty."""
    import services.tw_market_service as tw

    loaded = await tw.load_symbol_map_from_db()

    assert loaded is False
    assert tw.get_company_name("2330") is None


@pytest.mark.asyncio
async def test_load_symbol_map_from_db_tolerates_connection_failure():
    """An exception during DB read must not crash startup — the
    refresh step still runs and rebuilds from the upstream."""
    import services.tw_market_service as tw

    with patch("db.session.AsyncSessionLocal",
               side_effect=RuntimeError("connection refused")):
        loaded = await tw.load_symbol_map_from_db()

    assert loaded is False


@pytest.mark.asyncio
async def test_load_symbol_map_from_db_skips_blank_fields():
    """A row whose `industry` or `name_zh` is NULL must contribute to
    `_exchange_map` only — `_industry_map` / `_name_map` must not pick
    up `None`. Without this guard a partial DB seed would shadow the
    real name from a later TWSE refresh."""
    import services.tw_market_service as tw

    await _seed_rows([
        {"symbol": "9999", "exchange": "TWSE",
         "industry": None, "name_zh": None},
    ])

    loaded = await tw.load_symbol_map_from_db()

    assert loaded is True
    assert tw.get_exchange("9999") == "TWSE"
    assert tw.get_company_name("9999") is None
    assert tw.get_industry("9999") is None


@pytest.mark.asyncio
async def test_refresh_symbol_map_upserts_rows_to_db():
    """A successful TWSE refresh must write the new snapshot through
    to `tw_company_info` so the next cold-start can pick it up — even
    when Redis is unavailable."""
    import services.tw_market_service as tw
    from db.session import AsyncSessionLocal
    from models.tw_company_info import TwCompanyInfo

    twse_rows = [{"Code": "2330"}, {"Code": "2454"}]
    industry_rows = [
        {"symbol": "2330", "name_zh": "台積電", "industry": "半導體業"},
        {"symbol": "2454", "name_zh": "聯發科", "industry": "半導體業"},
    ]

    with patch.object(tw.twse, "get_all_twse_symbols",
                      AsyncMock(return_value=twse_rows)), \
         patch.object(tw.twse, "get_all_tpex_symbols",
                      AsyncMock(return_value=[])), \
         patch.object(tw.twse, "get_listed_company_industries",
                      AsyncMock(return_value=industry_rows)):
        await tw.refresh_symbol_map()

    async with AsyncSessionLocal() as db:
        rows = (await db.scalars(
            select(TwCompanyInfo).order_by(TwCompanyInfo.symbol)
        )).all()

    by_sym = {r.symbol: r for r in rows}
    assert set(by_sym) == {"2330", "2454"}
    assert by_sym["2330"].name_zh == "台積電"
    assert by_sym["2330"].industry == "半導體業"
    assert by_sym["2454"].exchange == "TWSE"


@pytest.mark.asyncio
async def test_refresh_symbol_map_upsert_overwrites_existing_row():
    """A re-run after a name change in TWSE must overwrite the
    existing row in place — same (symbol) PK, fresher `updated_at`,
    new `name_zh`. Without ON CONFLICT DO UPDATE the second refresh
    would either crash with a uniqueness violation or silently leave
    the stale row."""
    import services.tw_market_service as tw
    from db.session import AsyncSessionLocal
    from models.tw_company_info import TwCompanyInfo

    await _seed_rows([
        {
            "symbol": "2330", "exchange": "TWSE",
            "industry": "半導體業", "name_zh": "舊名稱",
            "updated_at": datetime(2020, 1, 1, tzinfo=UTC),
        },
    ])

    with patch.object(tw.twse, "get_all_twse_symbols",
                      AsyncMock(return_value=[{"Code": "2330"}])), \
         patch.object(tw.twse, "get_all_tpex_symbols",
                      AsyncMock(return_value=[])), \
         patch.object(tw.twse, "get_listed_company_industries",
                      AsyncMock(return_value=[
                          {"symbol": "2330", "name_zh": "台積電",
                           "industry": "半導體業"},
                      ])):
        await tw.refresh_symbol_map()

    async with AsyncSessionLocal() as db:
        row = (await db.scalars(
            select(TwCompanyInfo).where(TwCompanyInfo.symbol == "2330")
        )).one()
    assert row.name_zh == "台積電"
    # Comparing the timestamp directly trips SQLite's
    # naive-vs-aware mismatch on aiosqlite; year check sidesteps
    # the tz semantics and still proves the upsert refreshed the
    # row in place (rather than silently keeping the 2020 seed).
    assert row.updated_at.year > 2020


@pytest.mark.asyncio
async def test_refresh_symbol_map_tolerates_db_write_failure():
    """A DB outage during the persist step must not crash the
    upstream refresh — in-memory maps are still authoritative for
    this process; the DB tier is a recovery aid, not a hard
    dependency. Patch the session factory (the underlying dep
    inside the helper) so the helper's own try/except is what
    swallows the failure."""
    import services.tw_market_service as tw

    with patch.object(tw.twse, "get_all_twse_symbols",
                      AsyncMock(return_value=[{"Code": "2330"}])), \
         patch.object(tw.twse, "get_all_tpex_symbols",
                      AsyncMock(return_value=[])), \
         patch.object(tw.twse, "get_listed_company_industries",
                      AsyncMock(return_value=[
                          {"symbol": "2330", "name_zh": "台積電",
                           "industry": "半導體業"},
                      ])), \
         patch("db.session.AsyncSessionLocal",
               side_effect=RuntimeError("db down")):
        await tw.refresh_symbol_map()

    assert tw.get_company_name("2330") == "台積電"


@pytest.mark.asyncio
async def test_persist_symbol_map_to_db_is_silent_on_outage():
    """The persistence helper itself must swallow exceptions — its
    contract is best-effort, not hard-required."""
    import services.tw_market_service as tw

    with patch("db.session.AsyncSessionLocal",
               side_effect=RuntimeError("connection refused")):
        await tw._persist_symbol_map_to_db(
            {"2330": "TWSE"},
            {"2330": "半導體業"},
            {"2330": "台積電"},
        )
