"""Quote snapshots, tw_stock_futures_oi, tw_vix_daily."""
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import AsyncSessionLocal
from models.quote_snapshot import QuoteSnapshot
from models.tw_stock_futures_oi import TwStockFuturesOi
from services.ingest.repo._common import _chunked_upsert, _row_to_dict

log = logging.getLogger(__name__)


# ── Quote snapshots ────────────────────────────────────────────────

@dataclass(frozen=True)
class QuoteSnapshotRow:
    market: str
    symbol: str
    ts: datetime
    last_price: float | None
    change_pct: float | None
    prev_close: float | None
    volume: int | None
    source: str


def _utc_timestamp(value: datetime) -> float:
    """SQLite drops tzinfo, but quote snapshot timestamps are stored as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.timestamp()


async def insert_quote_snapshot(db: AsyncSession, snap: QuoteSnapshotRow) -> None:
    """Insert one quote snapshot. Same (market, symbol, ts) is a no-op
    instead of an error — the refresh task runs every 60 s and we'd
    rather drop a duplicate than crash if two pods race the lock."""
    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    row = {
        "market": snap.market,
        "symbol": snap.symbol,
        "ts": snap.ts,
        "last_price": snap.last_price,
        "change_pct": snap.change_pct,
        "prev_close": snap.prev_close,
        "volume": snap.volume,
        "source": snap.source,
    }
    if dialect == "sqlite":
        stmt = sqlite_insert(QuoteSnapshot).values(row).on_conflict_do_nothing(
            index_elements=["market", "symbol", "ts"],
        )
    else:
        stmt = pg_insert(QuoteSnapshot).values(row).on_conflict_do_nothing(
            index_elements=["market", "symbol", "ts"],
        )
    await db.execute(stmt)
    await db.commit()


async def read_latest_quote(
    db: AsyncSession, market: str, symbol: str, *, max_age_seconds: int,
) -> dict[str, Any] | None:
    """Return the most recent snapshot for (market, symbol) if it's within
    `max_age_seconds` of `now`. Returns None if no row found or stale.

    Output shape matches `tw_market_service._normalize_quote` so the
    caller can drop it straight into the response after attaching `tz`
    and `is_market_open`.
    """
    cutoff = datetime.now(UTC).timestamp() - max_age_seconds
    stmt = (
        select(QuoteSnapshot)
        .where(QuoteSnapshot.market == market, QuoteSnapshot.symbol == symbol)
        .order_by(QuoteSnapshot.ts.desc())
        .limit(1)
    )
    row = await db.scalar(stmt)
    if row is None or _utc_timestamp(row.ts) < cutoff:
        return None
    return {
        "symbol":      row.symbol,
        "market":      row.market,
        "price":       float(row.last_price) if row.last_price is not None else 0,
        "change_pct":  float(row.change_pct) if row.change_pct is not None else None,
        "prev_close":  float(row.prev_close) if row.prev_close is not None else None,
        "volume":      int(row.volume) if row.volume is not None else 0,
        "ts":          int(_utc_timestamp(row.ts) * 1000),
        "data_source": row.source,
    }


async def read_quote_snapshots_range(
    db: AsyncSession, market: str, symbol: str, *, start: datetime,
) -> list[tuple[datetime, float | None, int | None]]:
    """Narrow (ts, last_price, volume) fetch for the intraday aggregator.

    Returns rows ts-ascending with ts normalised to tz-aware UTC (SQLite
    drops tzinfo; Postgres keeps it). `volume` is the *cumulative* session
    volume as written by the quote refresh task — see
    `services.intraday_service` for the per-bar differencing.
    """
    stmt = (
        select(QuoteSnapshot.ts, QuoteSnapshot.last_price, QuoteSnapshot.volume)
        .where(
            QuoteSnapshot.market == market,
            QuoteSnapshot.symbol == symbol,
            QuoteSnapshot.ts >= start,
        )
        .order_by(QuoteSnapshot.ts.asc())
    )
    rows = (await db.execute(stmt)).all()
    out: list[tuple[datetime, float | None, int | None]] = []
    for ts, price, volume in rows:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        out.append((
            ts,
            float(price) if price is not None else None,
            int(volume) if volume is not None else None,
        ))
    return out


async def read_quote_snapshots_range_autosession(
    market: str, symbol: str, *, start: datetime,
) -> list[tuple[datetime, float | None, int | None]]:
    """Same as `read_quote_snapshots_range` but opens its own session and
    swallows DB errors — the intraday endpoint degrades to an empty bar
    list (with coverage note) rather than 500 on a Postgres outage."""
    try:
        async with AsyncSessionLocal() as db:
            return await read_quote_snapshots_range(db, market, symbol, start=start)
    except Exception as exc:
        log.warning("ingest.quote_snapshot.range_read_error",
                    extra={"market": market, "symbol": symbol, "error": str(exc)})
        return []


async def prune_quote_snapshots(db: AsyncSession, *, older_than_days: int) -> int:
    """Delete snapshots older than `older_than_days`. Returns deleted count."""
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    stmt = delete(QuoteSnapshot).where(QuoteSnapshot.ts < cutoff)
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount or 0


async def insert_quote_snapshot_autosession(snap: QuoteSnapshotRow) -> None:
    """Same as `insert_quote_snapshot` but opens its own session.

    Used by the TW refresh task which doesn't already have a session in
    scope. DB errors are swallowed because the snapshot is best-effort —
    the live WS push must not be blocked on a Postgres outage.
    """
    try:
        async with AsyncSessionLocal() as db:
            await insert_quote_snapshot(db, snap)
    except Exception as exc:
        log.warning("ingest.quote_snapshot.write_error",
                    extra={"market": snap.market, "symbol": snap.symbol, "error": str(exc)})


async def read_latest_quote_autosession(
    market: str, symbol: str, *, max_age_seconds: int,
) -> dict[str, Any] | None:
    """Same as `read_latest_quote` but opens its own session and
    swallows DB errors so the read path falls through cleanly to upstream."""
    try:
        async with AsyncSessionLocal() as db:
            return await read_latest_quote(
                db, market, symbol, max_age_seconds=max_age_seconds,
            )
    except Exception as exc:
        log.warning("ingest.quote_snapshot.read_error",
                    extra={"market": market, "symbol": symbol, "error": str(exc)})
        return None


# ── PR #282: tw_stock_futures_oi ─────────────────────────────────


@dataclass(frozen=True)
class StockFuturesOiRow:
    """One per-stock-futures aggregated row (all 3 institutional
    types collapsed into columns, mirroring InstitutionalDailyRow).
    The cron builds these from FinMind's per-investor-type rows."""
    market: str
    symbol: str
    contract_id: str
    ts: date
    fini_long_oi: int | None
    fini_short_oi: int | None
    fini_net_oi: int | None
    sitc_long_oi: int | None
    sitc_short_oi: int | None
    sitc_net_oi: int | None
    dealer_long_oi: int | None
    dealer_short_oi: int | None
    dealer_net_oi: int | None
    source: str


_STOCK_FUTURES_OI_FIELDS = (
    "market", "symbol", "contract_id", "ts",
    "fini_long_oi", "fini_short_oi", "fini_net_oi",
    "sitc_long_oi", "sitc_short_oi", "sitc_net_oi",
    "dealer_long_oi", "dealer_short_oi", "dealer_net_oi",
    "source",
)


async def upsert_stock_futures_oi(
    db: AsyncSession, rows: Iterable[StockFuturesOiRow],
) -> int:
    """Bulk upsert. ON CONFLICT (market, symbol, ts) updates every
    metric column + contract_id + source so a re-ingest on the
    same date overwrites stale values (e.g. when FinMind corrects
    a prior session's OI numbers)."""
    payload = [
        _row_to_dict(r, fields=_STOCK_FUTURES_OI_FIELDS) for r in rows
    ]
    return await _chunked_upsert(
        db,
        model=TwStockFuturesOi,
        payload=payload,
        index_elements=["market", "symbol", "ts"],
        update_cols=(
            "contract_id",
            "fini_long_oi", "fini_short_oi", "fini_net_oi",
            "sitc_long_oi", "sitc_short_oi", "sitc_net_oi",
            "dealer_long_oi", "dealer_short_oi", "dealer_net_oi",
            "source",
        ),
    )


async def read_top_foreign_stock_futures_buyers(
    db: AsyncSession, *,
    market: str = "TW",
    days: int = 5,
    limit: int = 10,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    """Top N stocks by foreign-investor net-OI **change** over the
    last `days` trading days ending at `as_of` (default: today).

    Computes (latest_fini_net_oi - earliest_fini_net_oi) for each
    symbol that has both endpoints and ranks descending. Negative
    deltas (foreign net-OI shrinking, i.e. shorts building) are
    NOT inverted — the read tier returns the most-bullish only.
    Caller (context block) can call again with a different sort
    if it wants the bearish side too.

    Backtest look-ahead protection (PR #282 mirrors PR #243): when
    `as_of` is set, NOTHING is masked — futures OI rows are
    insert-only on FinMind's side (no retroactive corrections to
    long/short/net), and the cron ingests one-row-per-day with
    immutable historical numbers. Personas reading at as_of=D see
    exactly the OI snapshot that was published on D's close.
    """
    end = as_of or date.today()
    cutoff = end - timedelta(days=days * 2)   # generous: covers weekends
    stmt = (
        select(
            TwStockFuturesOi.symbol,
            TwStockFuturesOi.ts,
            TwStockFuturesOi.fini_net_oi,
            TwStockFuturesOi.fini_long_oi,
            TwStockFuturesOi.fini_short_oi,
            TwStockFuturesOi.contract_id,
        )
        .where(
            TwStockFuturesOi.market == market,
            TwStockFuturesOi.ts >= cutoff,
            TwStockFuturesOi.ts <= end,
            TwStockFuturesOi.fini_net_oi.isnot(None),
        )
        .order_by(
            TwStockFuturesOi.symbol.asc(),
            TwStockFuturesOi.ts.asc(),
        )
    )
    rows = (await db.execute(stmt)).all()
    if not rows:
        return []

    # Group by symbol, then take (latest, earliest) within the
    # window for each.
    by_symbol: dict[str, list[tuple[date, int, int | None, int | None, str]]] = {}
    for sym, ts, net, long_oi, short_oi, contract in rows:
        by_symbol.setdefault(sym, []).append(
            (ts, int(net), long_oi, short_oi, contract),
        )

    results: list[dict[str, Any]] = []
    for sym, series in by_symbol.items():
        if len(series) < 2:
            # Need at least 2 points to compute a delta; one-row
            # symbols (just-listed contracts) get skipped silently.
            continue
        series.sort(key=lambda x: x[0])
        first_ts, first_net, *_ = series[0]
        last_ts, last_net, last_long, last_short, last_contract = series[-1]
        if last_ts == first_ts:
            continue
        delta = last_net - first_net
        results.append({
            "symbol":          sym,
            "contract_id":     last_contract,
            "fini_net_oi":     last_net,
            "fini_long_oi":    last_long,
            "fini_short_oi":   last_short,
            "fini_change":     delta,
            "as_of":           last_ts.isoformat(),
            "from_ts":         first_ts.isoformat(),
        })

    results.sort(key=lambda r: r["fini_change"], reverse=True)
    return results[:limit]


# ── PR #283: tw_vix_daily ────────────────────────────────────────


@dataclass(frozen=True)
class VixDailyRow:
    """One day's TAIWAN VIX close."""
    market: str
    ts: date
    vix_value: float
    source: str


_VIX_FIELDS = ("market", "ts", "vix_value", "source")


async def upsert_tw_vix_daily(
    db: AsyncSession, rows: Iterable[VixDailyRow],
) -> int:
    """Bulk upsert. ON CONFLICT (market, ts) updates `vix_value` +
    `source` so a re-pull of an already-ingested day with corrected
    TAIFEX numbers overwrites in place."""
    from models.tw_vix_daily import TwVixDaily
    payload = [_row_to_dict(r, fields=_VIX_FIELDS) for r in rows]
    return await _chunked_upsert(
        db,
        model=TwVixDaily,
        payload=payload,
        index_elements=["market", "ts"],
        update_cols=("vix_value", "source"),
    )


_VIX_REGIME_WINDOW_DAYS = 365
# Below this many sessions a percentile is noise dressed up as a
# statistic — 20 trading days is one month. The snapshot still
# reports `sample_days` so the prompt can say "history too short"
# instead of silently omitting the field (an omitted block reads to
# the model as "not relevant", not as "unknown").
_VIX_REGIME_MIN_SAMPLES = 20


def _percentile_rank(values: list[float], current: float) -> float:
    """Share of `values` at or below `current`, as 0-100."""
    at_or_below = sum(1 for v in values if v <= current)
    return round(at_or_below / len(values) * 100, 1)


def _quantile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank quantile — matches the ad-hoc awk check used when
    this regression was diagnosed, so DB and hand-audit agree."""
    idx = min(len(sorted_values) - 1, int(len(sorted_values) * q))
    return round(sorted_values[idx], 4)


async def _read_vix_regime(
    db: AsyncSession, *,
    market: str,
    end: date,
    current: float,
    window_days: int = _VIX_REGIME_WINDOW_DAYS,
) -> dict[str, Any]:
    """Where `current` sits in the trailing `window_days` of closes.

    Personas used to judge the TAIWAN VIX against a hardcoded
    "median ≈ 16-18" written into the prompt. That baseline predates
    the current volatility regime: over 2026-04..07 the median close
    was 36.57 and the *minimum* was 27.32, so a 36.14 print — the 39th
    percentile, i.e. calmer than usual — was being read as extreme
    panic and suppressing every recommendation. The regime block
    replaces that constant with the distribution actually observed.

    Backtest semantics: clamped to `<= end`, same as the caller, so a
    replay can't rank today's value against volatility it hadn't seen.
    """
    from models.tw_vix_daily import TwVixDaily

    stmt = (
        select(TwVixDaily.vix_value)
        .where(
            TwVixDaily.market == market,
            TwVixDaily.ts >= end - timedelta(days=window_days),
            TwVixDaily.ts <= end,
        )
    )
    values = sorted(
        float(v) for (v,) in (await db.execute(stmt)).all()
    )
    sample_days = len(values)
    sufficient = sample_days >= _VIX_REGIME_MIN_SAMPLES
    regime: dict[str, Any] = {
        "sample_days": sample_days,
        "window_days": window_days,
        "sufficient": sufficient,
    }
    if sufficient:
        regime |= {
            "percentile": _percentile_rank(values, current),
            "median":     _quantile(values, 0.50),
            "p25":        _quantile(values, 0.25),
            "p75":        _quantile(values, 0.75),
        }
    else:
        # `_minify_for_prompt` strips None values, so the nulls below
        # never reach the model — `sufficient` + `sample_days` + this
        # note are what it actually sees. Say the gap out loud: an
        # absent statistic reads as "not relevant", and the personas
        # fill that silence with a remembered number, which is the
        # exact failure this block exists to end.
        regime |= {
            "percentile": None, "median": None,
            "p25": None, "p75": None,
            "note": (
                f"VIX 歷史僅 {sample_days} 個交易日"
                f"（需 {_VIX_REGIME_MIN_SAMPLES} 日）"
                "，尚不足以判斷目前水位的相對高低；"
                "請勿引用任何歷史中位數或恐慌門檻。"
            ),
        }
    return regime


async def read_tw_vix_snapshot(
    db: AsyncSession, *,
    market: str = "TW",
    days: int = 5,
    as_of: date | None = None,
) -> dict[str, Any] | None:
    """Latest VIX value + value `days` trading-days ago (best-effort
    via calendar-day lookback) + change % + where that value sits in
    the trailing year's distribution (`regime`).

    Returns None when the archive has no rows in the window — caller
    (ctx block) drops the field entirely so personas don't reference
    a phantom value.

    Backtest semantics: when `as_of` is set, all lookups are
    clamped to `<= as_of`. The TAIFEX archive is insert-only (VIX
    closes don't get retroactively corrected the way some FinMind
    datasets do), so no extra masking needed.
    """
    from models.tw_vix_daily import TwVixDaily

    end = as_of or date.today()
    cutoff = end - timedelta(days=days * 3 + 14)
    stmt = (
        select(TwVixDaily.ts, TwVixDaily.vix_value)
        .where(
            TwVixDaily.market == market,
            TwVixDaily.ts >= cutoff,
            TwVixDaily.ts <= end,
        )
        .order_by(TwVixDaily.ts.asc())
    )
    rows = (await db.execute(stmt)).all()
    if not rows:
        return None

    series = [(r[0], float(r[1])) for r in rows]
    last_ts, last_value = series[-1]

    # Pick the closest bar at-or-before `end - days` calendar days.
    # We over-approximate by iterating from oldest to newest because
    # the series is short (< 25 rows even at days=5 + 14 buffer).
    target = last_ts - timedelta(days=days)
    prev_pair = None
    for ts, val in series:
        if ts <= target:
            prev_pair = (ts, val)
        else:
            break

    change_pct: float | None = None
    if prev_pair is not None and prev_pair[1]:
        change_pct = round(
            (last_value / prev_pair[1] - 1) * 100, 4,
        )

    return {
        "as_of":      last_ts.isoformat(),
        "value":      round(last_value, 4),
        "from_ts":    prev_pair[0].isoformat() if prev_pair else None,
        "from_value": round(prev_pair[1], 4) if prev_pair else None,
        "change_pct": change_pct,
        "regime":     await _read_vix_regime(
            db, market=market, end=end, current=last_value,
        ),
    }
