"""
Watchlist API.

GET    /api/watchlist                          list all watchlists (with live prices)
POST   /api/watchlist                          create watchlist
DELETE /api/watchlist/{wid}                    delete watchlist + items

POST   /api/watchlist/{wid}/items              add symbol
DELETE /api/watchlist/{wid}/items/{item_id}    remove symbol
"""
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.watchlist.schemas import WatchlistCreate, WatchlistItemAdd, WatchlistOut, WatchlistItemOut
from auth.permissions import require_viewer
from db.session import get_db
import services.watchlist_service as svc
from services.watchlist_service import DuplicateWatchlistItem

router = APIRouter()
CurrentUser = Annotated[dict, Depends(require_viewer)]
DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=list[WatchlistOut])
async def list_watchlists(user: CurrentUser, db: DB):
    return await svc.list_watchlists(db, user["id"])


@router.post("", response_model=WatchlistOut, status_code=201)
async def create_watchlist(body: WatchlistCreate, user: CurrentUser, db: DB):
    return await svc.create_watchlist(db, user["id"], body.name)


@router.delete("/{wid}", status_code=204)
async def delete_watchlist(wid: str, user: CurrentUser, db: DB):
    ok = await svc.delete_watchlist(db, user["id"], wid)
    if not ok:
        raise HTTPException(status_code=404, detail="Watchlist not found")


@router.post("/{wid}/items", response_model=WatchlistItemOut, status_code=201)
async def add_item(wid: str, body: WatchlistItemAdd, user: CurrentUser, db: DB):
    # Pydantic regex on WatchlistItemAdd.market already constrains to
    # (US|TW|CRYPTO) — no need to re-check here.
    try:
        item = await svc.add_item(db, user["id"], wid, body.symbol, body.market)
    except DuplicateWatchlistItem as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if item is None:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    return item


@router.delete("/{wid}/items/{item_id}", status_code=204)
async def remove_item(wid: str, item_id: str, user: CurrentUser, db: DB):
    ok = await svc.remove_item(db, user["id"], wid, item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
