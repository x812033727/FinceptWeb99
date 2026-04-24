from pydantic import BaseModel, Field


class WatchlistCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class WatchlistItemAdd(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$")
    market: str = Field(..., pattern=r"^(US|TW|us|tw)$")


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
