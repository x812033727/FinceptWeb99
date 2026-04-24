from pydantic import BaseModel


class WatchlistCreate(BaseModel):
    name: str


class WatchlistItemAdd(BaseModel):
    symbol: str
    market: str   # "US" | "TW"


class WatchlistItemOut(BaseModel):
    id: str
    symbol: str
    market: str
    added_at: str
    # enriched live data (optional, may be null if quote fails)
    price: float | None = None
    change_pct: float | None = None
    name: str | None = None


class WatchlistOut(BaseModel):
    id: str
    name: str
    created_at: str
    items: list[WatchlistItemOut] = []
