"""
Pure unit tests for watchlist_service — CRUD and live-quote enrichment.

All DB work uses the in-process SQLite test engine (db_session fixture).
Quote fetching is mocked so no external services are hit.
"""
import uuid
import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User, UserRole
from models.watchlist import Watchlist, WatchlistItem
from models.portfolio import Market
from services.watchlist_service import (
    _enrich_item,
    _wl_to_dict,
    list_watchlists,
    create_watchlist,
    delete_watchlist,
    add_item,
    remove_item,
)


# ── helpers ─────────────────────────────────────────────────────────

async def _make_user(db: AsyncSession) -> User:
    u = User(
        email=f"wl_{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.viewer,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _make_watchlist(db: AsyncSession, user_id: uuid.UUID, name: str = "Test") -> Watchlist:
    wl = Watchlist(user_id=user_id, name=name)
    db.add(wl)
    await db.commit()
    await db.refresh(wl)
    return wl


async def _make_item(
    db: AsyncSession,
    watchlist_id: uuid.UUID,
    symbol: str = "AAPL",
    market: Market = Market.US,
) -> WatchlistItem:
    item = WatchlistItem(watchlist_id=watchlist_id, symbol=symbol, market=market)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


# ── _wl_to_dict ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wl_to_dict_shape(db_session: AsyncSession):
    user = await _make_user(db_session)
    wl = await _make_watchlist(db_session, user.id, "My WL")
    result = _wl_to_dict(wl, [])
    assert result["name"] == "My WL"
    assert result["id"] == str(wl.id)
    assert result["items"] == []
    assert "created_at" in result


# ── _enrich_item ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enrich_item_us_market(db_session: AsyncSession):
    user = await _make_user(db_session)
    wl = await _make_watchlist(db_session, user.id)
    item = await _make_item(db_session, wl.id, "AAPL", Market.US)

    mock_quote = {"price": 182.5, "change_pct": 1.2, "name": "Apple Inc."}
    # Patch the lazy-imported get_quote at its source module (the import inside
    # _enrich_item executes at call time, so patching the source is effective).
    with patch("services.us_market_service.get_quote", new_callable=AsyncMock) as m:
        m.return_value = mock_quote
        result = await _enrich_item(item)

    assert result["symbol"] == "AAPL"
    assert result["market"] == "US"
    assert result["price"] == 182.5
    assert result["change_pct"] == 1.2


@pytest.mark.asyncio
async def test_enrich_item_tw_market(db_session: AsyncSession):
    user = await _make_user(db_session)
    wl = await _make_watchlist(db_session, user.id)
    item = await _make_item(db_session, wl.id, "2330", Market.TW)

    mock_quote = {"price": 610.0, "change_pct": 0.5, "name_zh": "台積電"}
    with patch("services.tw_market_service.get_quote", new_callable=AsyncMock) as m:
        m.return_value = mock_quote
        result = await _enrich_item(item)

    assert result["symbol"] == "2330"
    assert result["market"] == "TW"
    assert result["name"] == "台積電"


@pytest.mark.asyncio
async def test_enrich_item_quote_error_returns_none_fields(db_session: AsyncSession):
    """If quote fetch raises, price/change_pct/name remain None instead of crashing."""
    user = await _make_user(db_session)
    wl = await _make_watchlist(db_session, user.id)
    item = await _make_item(db_session, wl.id, "FAKE", Market.US)

    with patch("services.us_market_service.get_quote", new_callable=AsyncMock) as m:
        m.side_effect = RuntimeError("network error")
        result = await _enrich_item(item)

    assert result["symbol"] == "FAKE"
    assert result["price"] is None
    assert result["change_pct"] is None


@pytest.mark.asyncio
async def test_enrich_item_surfaces_quoted_at_and_data_source(
    db_session: AsyncSession,
):
    """The watchlist UI shows "資料時間 14:31" + a data-source badge so
    users know whether they're looking at live, EOD, or stale data.
    Both fields are forwarded from the quote service's `ts` (epoch ms)
    + `data_source`."""
    user = await _make_user(db_session)
    wl = await _make_watchlist(db_session, user.id)
    item = await _make_item(db_session, wl.id, "2330", Market.TW)

    # 2025-05-01 06:31:00 UTC = 1746081060000 ms (actual decoded value
    # — the original comment claimed 2026-04-30 but the math doesn't
    # match; what matters is the round-trip from epoch ms back to ISO
    # is correct, regardless of which calendar day it lands on).
    mock_quote = {
        "price": 610.0,
        "change_pct": 0.5,
        "name_zh": "台積電",
        "ts": 1746081060000,
        "data_source": "twse",
    }
    with patch("services.tw_market_service.get_quote", new_callable=AsyncMock) as m:
        m.return_value = mock_quote
        result = await _enrich_item(item)

    assert result["data_source"] == "twse"
    assert result["quoted_at"] is not None
    assert result["quoted_at"].startswith("2025-05-01T06:31:00")
    # TW market reads `name_zh` from the quote into `name`.
    assert result["name"] == "台積電"


@pytest.mark.asyncio
async def test_enrich_item_returns_blank_quote_when_upstream_raises(
    db_session: AsyncSession,
):
    """A single watchlist item's quote fetch failure must NOT propagate
    out of `_enrich_item` and break `list_watchlists`'s gather. The
    item should land with `price=None / change_pct=None / data_source=None`
    so the rest of the watchlist still renders."""
    user = await _make_user(db_session)
    wl = await _make_watchlist(db_session, user.id, "Resilient")
    item = await _make_item(db_session, wl.id, "2330", Market.TW)

    with patch(
        "services.tw_market_service.get_quote",
        new=AsyncMock(side_effect=RuntimeError("upstream chaos")),
    ):
        result = await _enrich_item(item)

    assert result["symbol"] == "2330"
    assert result["price"] is None
    assert result["change_pct"] is None
    assert result["data_source"] is None


# ── create_watchlist ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_watchlist_persists(db_session: AsyncSession):
    user = await _make_user(db_session)
    result = await create_watchlist(db_session, str(user.id), "Portfolio Watch")
    assert result["name"] == "Portfolio Watch"
    assert "id" in result
    assert result["items"] == []


@pytest.mark.asyncio
async def test_create_multiple_watchlists_same_user(db_session: AsyncSession):
    user = await _make_user(db_session)
    await create_watchlist(db_session, str(user.id), "List A")
    await create_watchlist(db_session, str(user.id), "List B")
    lists = await list_watchlists(db_session, str(user.id))
    names = {w["name"] for w in lists}
    assert "List A" in names
    assert "List B" in names


# ── list_watchlists ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_watchlists_empty_for_new_user(db_session: AsyncSession):
    user = await _make_user(db_session)
    with patch("services.us_market_service.get_quote", new_callable=AsyncMock):
        result = await list_watchlists(db_session, str(user.id))
    assert result == []


@pytest.mark.asyncio
async def test_list_watchlists_user_isolation(db_session: AsyncSession):
    """User A's watchlists must not appear for user B."""
    u1 = await _make_user(db_session)
    u2 = await _make_user(db_session)
    await create_watchlist(db_session, str(u1.id), "User1 Only")

    with patch("services.us_market_service.get_quote", new_callable=AsyncMock):
        u2_lists = await list_watchlists(db_session, str(u2.id))

    assert u2_lists == []


@pytest.mark.asyncio
async def test_list_watchlists_includes_enriched_items(db_session: AsyncSession):
    user = await _make_user(db_session)
    wl = await _make_watchlist(db_session, user.id, "Tech")
    await _make_item(db_session, wl.id, "NVDA", Market.US)

    mock_quote = {"price": 900.0, "change_pct": 2.1, "name": "Nvidia"}
    with patch("services.us_market_service.get_quote", new_callable=AsyncMock) as m:
        m.return_value = mock_quote
        lists = await list_watchlists(db_session, str(user.id))

    assert len(lists) == 1
    items = lists[0]["items"]
    assert len(items) == 1
    assert items[0]["symbol"] == "NVDA"


# ── delete_watchlist ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_watchlist_own(db_session: AsyncSession):
    user = await _make_user(db_session)
    wl = await _make_watchlist(db_session, user.id)
    ok = await delete_watchlist(db_session, str(user.id), str(wl.id))
    assert ok is True

    with patch("services.us_market_service.get_quote", new_callable=AsyncMock):
        remaining = await list_watchlists(db_session, str(user.id))
    assert all(w["id"] != str(wl.id) for w in remaining)


@pytest.mark.asyncio
async def test_delete_watchlist_other_user_returns_false(db_session: AsyncSession):
    u1 = await _make_user(db_session)
    u2 = await _make_user(db_session)
    wl = await _make_watchlist(db_session, u1.id)
    ok = await delete_watchlist(db_session, str(u2.id), str(wl.id))
    assert ok is False


@pytest.mark.asyncio
async def test_delete_watchlist_nonexistent_returns_false(db_session: AsyncSession):
    user = await _make_user(db_session)
    ok = await delete_watchlist(db_session, str(user.id), str(uuid.uuid4()))
    assert ok is False


# ── add_item ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_item_returns_enriched_dict(db_session: AsyncSession):
    user = await _make_user(db_session)
    wl = await _make_watchlist(db_session, user.id)

    mock_quote = {"price": 155.0, "change_pct": -0.3, "name": "Meta"}
    with patch("services.us_market_service.get_quote", new_callable=AsyncMock) as m:
        m.return_value = mock_quote
        item = await add_item(db_session, str(user.id), str(wl.id), "META", "US")

    assert item is not None
    assert item["symbol"] == "META"
    assert item["market"] == "US"
    assert "id" in item


@pytest.mark.asyncio
async def test_add_item_wrong_watchlist_returns_none(db_session: AsyncSession):
    user = await _make_user(db_session)
    result = await add_item(db_session, str(user.id), str(uuid.uuid4()), "AAPL", "US")
    assert result is None


@pytest.mark.asyncio
async def test_add_item_symbol_uppercased(db_session: AsyncSession):
    user = await _make_user(db_session)
    wl = await _make_watchlist(db_session, user.id)

    with patch("services.us_market_service.get_quote", new_callable=AsyncMock) as m:
        m.return_value = {"price": 1.0, "change_pct": 0.0, "name": ""}
        item = await add_item(db_session, str(user.id), str(wl.id), "aapl", "US")

    assert item["symbol"] == "AAPL"


# ── remove_item ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_remove_item_own(db_session: AsyncSession):
    user = await _make_user(db_session)
    wl = await _make_watchlist(db_session, user.id)
    db_item = await _make_item(db_session, wl.id, "TSLA", Market.US)

    ok = await remove_item(db_session, str(user.id), str(wl.id), str(db_item.id))
    assert ok is True


@pytest.mark.asyncio
async def test_remove_item_other_user_returns_false(db_session: AsyncSession):
    u1 = await _make_user(db_session)
    u2 = await _make_user(db_session)
    wl = await _make_watchlist(db_session, u1.id)
    db_item = await _make_item(db_session, wl.id, "TSLA", Market.US)

    ok = await remove_item(db_session, str(u2.id), str(wl.id), str(db_item.id))
    assert ok is False


@pytest.mark.asyncio
async def test_remove_item_nonexistent_returns_false(db_session: AsyncSession):
    user = await _make_user(db_session)
    wl = await _make_watchlist(db_session, user.id)
    ok = await remove_item(db_session, str(user.id), str(wl.id), str(uuid.uuid4()))
    assert ok is False
