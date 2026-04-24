"""
Watchlist service — CRUD + live price enrichment.
"""
import asyncio
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from models.watchlist import Watchlist, WatchlistItem
from models.portfolio import Market


# ── helpers ────────────────────────────────────────────────────────

def _wl_to_dict(wl: Watchlist, items_out: list[dict]) -> dict:
    return {
        "id": str(wl.id),
        "name": wl.name,
        "created_at": wl.created_at.isoformat(),
        "items": items_out,
    }


async def _enrich_item(item: WatchlistItem) -> dict[str, Any]:
    """Fetch live quote for a single watchlist item."""
    out: dict[str, Any] = {
        "id": str(item.id),
        "symbol": item.symbol,
        "market": item.market.value,
        "added_at": item.added_at.isoformat(),
        "price": None,
        "change_pct": None,
        "name": None,
    }
    try:
        if item.market == Market.US:
            from services.us_market_service import get_quote
            q = await get_quote(item.symbol)
            out["price"] = q.get("price")
            out["change_pct"] = q.get("change_pct")
            out["name"] = q.get("name")
        else:
            from services.tw_market_service import get_quote
            q = await get_quote(item.symbol)
            out["price"] = q.get("price")
            out["change_pct"] = q.get("change_pct")
            out["name"] = q.get("name_zh")
    except Exception:
        pass
    return out


# ── CRUD ───────────────────────────────────────────────────────────

async def list_watchlists(db: AsyncSession, user_id: str) -> list[dict]:
    uid = uuid.UUID(user_id)
    result = await db.execute(
        select(Watchlist)
        .where(Watchlist.user_id == uid)
        .options(selectinload(Watchlist.items))
        .order_by(Watchlist.created_at)
    )
    watchlists = result.scalars().all()
    out = []
    for wl in watchlists:
        items_out = await asyncio.gather(*[_enrich_item(i) for i in wl.items])
        out.append(_wl_to_dict(wl, list(items_out)))
    return out


async def create_watchlist(db: AsyncSession, user_id: str, name: str) -> dict:
    wl = Watchlist(user_id=uuid.UUID(user_id), name=name)
    db.add(wl)
    await db.commit()
    await db.refresh(wl)
    return _wl_to_dict(wl, [])


async def delete_watchlist(db: AsyncSession, user_id: str, watchlist_id: str) -> bool:
    uid = uuid.UUID(user_id)
    wid = uuid.UUID(watchlist_id)
    result = await db.execute(
        select(Watchlist).where(Watchlist.id == wid, Watchlist.user_id == uid)
    )
    wl = result.scalar_one_or_none()
    if not wl:
        return False
    await db.delete(wl)
    await db.commit()
    return True


async def add_item(
    db: AsyncSession, user_id: str, watchlist_id: str, symbol: str, market: str
) -> dict | None:
    uid = uuid.UUID(user_id)
    wid = uuid.UUID(watchlist_id)
    result = await db.execute(
        select(Watchlist).where(Watchlist.id == wid, Watchlist.user_id == uid)
    )
    wl = result.scalar_one_or_none()
    if not wl:
        return None

    item = WatchlistItem(
        watchlist_id=wid,
        symbol=symbol.upper(),
        market=Market(market.upper()),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return await _enrich_item(item)


async def remove_item(
    db: AsyncSession, user_id: str, watchlist_id: str, item_id: str
) -> bool:
    uid = uuid.UUID(user_id)
    wid = uuid.UUID(watchlist_id)
    iid = uuid.UUID(item_id)
    # Verify ownership via join
    result = await db.execute(
        select(WatchlistItem)
        .join(Watchlist)
        .where(
            WatchlistItem.id == iid,
            WatchlistItem.watchlist_id == wid,
            Watchlist.user_id == uid,
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        return False
    await db.delete(item)
    await db.commit()
    return True
