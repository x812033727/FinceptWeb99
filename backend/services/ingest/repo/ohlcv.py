"""OHLCV bar reads/writes for the scheduled-fetch subsystem."""
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import AsyncSessionLocal
from models.ohlcv_daily import OhlcvDaily
from services.ingest.repo._common import _chunked_upsert

log = logging.getLogger(__name__)


# ── OHLCV ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OhlcvBar:
    market: str
    symbol: str
    ts: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    source: str

    @classmethod
    def from_connector_row(
        cls, market: str, symbol: str, source: str, row: dict[str, Any],
    ) -> "OhlcvBar | None":
        """Coerce a connector dict (`{time, open, high, low, close, volume}`)
        into a typed OhlcvBar. Returns None for malformed rows so callers
        can `filter(None, ...)` instead of try/except per row.

        Drops rows with non-positive ``close`` or ``open`` — listed
        equities never trade at 0 and an upstream zero is almost
        always a parser swallowing a placeholder ('—', 'N/A'). Logs
        each dropped row at WARNING so operators can see how many
        junk rows the upstream is emitting today.
        """
        ts_raw = row.get("time") or row.get("date")
        if not ts_raw:
            return None
        try:
            ts = date.fromisoformat(str(ts_raw)[:10])
        except ValueError:
            return None
        close = _to_float(row.get("close"))
        open_ = _to_float(row.get("open"))
        if close is not None and close <= 0:
            log.warning(
                "ohlcv.non_positive_close",
                extra={
                    "market": market, "symbol": symbol, "source": source,
                    "ts": ts.isoformat(), "close": close,
                },
            )
            return None
        if open_ is not None and open_ <= 0:
            log.warning(
                "ohlcv.non_positive_open",
                extra={
                    "market": market, "symbol": symbol, "source": source,
                    "ts": ts.isoformat(), "open": open_,
                },
            )
            return None
        return cls(
            market=market,
            symbol=symbol,
            ts=ts,
            open=open_,
            high=_to_float(row.get("high")),
            low=_to_float(row.get("low")),
            close=close,
            volume=_to_int(row.get("volume")),
            source=source,
        )


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _bar_to_row(bar: OhlcvBar) -> dict[str, Any]:
    return {
        "market": bar.market,
        "symbol": bar.symbol,
        "ts": bar.ts,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "source": bar.source,
    }


async def upsert_ohlcv_bars(db: AsyncSession, bars: Iterable[OhlcvBar]) -> int:
    """Bulk upsert OHLCV bars. Returns number of rows passed in.

    Uses dialect-specific ON CONFLICT for Postgres + SQLite (the only two
    dialects this project runs on). Bars with the same (market, symbol, ts)
    are last-write-wins on the price columns + source. `ingested_at` is
    refreshed on conflict so operators can tell when a bar was last
    re-ingested.
    """
    payload = [_bar_to_row(b) for b in bars]
    return await _chunked_upsert(
        db,
        model=OhlcvDaily,
        payload=payload,
        index_elements=["market", "symbol", "ts"],
        update_cols=("open", "high", "low", "close", "volume", "source"),
    )


async def read_ohlcv_range(
    db: AsyncSession,
    market: str,
    symbol: str,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """Return bars in `[start, end]` (inclusive) ordered ascending by ts.

    Output shape matches the connector layer (`time` ISO string, numeric
    OHLCV) so it's a drop-in for `tw_market_service.get_history`.
    """
    stmt = (
        select(OhlcvDaily)
        .where(
            OhlcvDaily.market == market,
            OhlcvDaily.symbol == symbol,
            OhlcvDaily.ts >= start,
            OhlcvDaily.ts <= end,
        )
        .order_by(OhlcvDaily.ts.asc())
    )
    rows = (await db.scalars(stmt)).all()
    return [
        {
            "time":   r.ts.isoformat(),
            "open":   float(r.open) if r.open is not None else None,
            "high":   float(r.high) if r.high is not None else None,
            "low":    float(r.low) if r.low is not None else None,
            "close":  float(r.close) if r.close is not None else None,
            "volume": int(r.volume) if r.volume is not None else 0,
        }
        for r in rows
    ]


# ── Convenience: open a new session for ad-hoc reads ───────────────

async def read_ohlcv_range_autosession(
    market: str, symbol: str, start: date, end: date,
) -> list[dict[str, Any]]:
    """Same as `read_ohlcv_range` but opens its own DB session.

    Used by `tw_market_service` whose existing read path doesn't already
    have a session in scope. DB errors are caught and logged so the
    service can fall through to the upstream waterfall — DB outages
    must never break the read path.
    """
    try:
        async with AsyncSessionLocal() as db:
            return await read_ohlcv_range(db, market, symbol, start, end)
    except Exception as exc:
        log.warning("ingest.read.db_error",
                    extra={"market": market, "symbol": symbol, "error": str(exc)})
        return []


async def upsert_ohlcv_bars_autosession(bars: Iterable[OhlcvBar]) -> int:
    """Same as `upsert_ohlcv_bars` but opens its own DB session.

    Used by `tw_market_service.get_history` to write back the upstream
    response without threading a session through the call chain. Errors
    are swallowed because the read path's primary obligation is to
    return data — DB writes are best-effort.
    """
    bars = list(bars)
    if not bars:
        return 0
    try:
        async with AsyncSessionLocal() as db:
            return await upsert_ohlcv_bars(db, bars)
    except Exception as exc:
        log.warning("ingest.write.db_error",
                    extra={"market": bars[0].market, "count": len(bars), "error": str(exc)})
        return 0
