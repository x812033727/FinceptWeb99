"""Central repository for the scheduled-fetch subsystem.

Phase 1 covers OHLCV reads/writes plus a Redis-backed health snapshot
the admin UI can poll. Schedulers write here; `tw_market_service` reads
here as the new tier between Redis and the upstream waterfall.
"""
import hashlib
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_cache import cache_delete, cache_get, cache_incr, cache_set, get_redis
from db.session import AsyncSessionLocal
from models.fundamentals_snapshot import FundamentalsSnapshot
from models.news_article import NewsArticle
from models.ohlcv_daily import OhlcvDaily
from models.quote_snapshot import QuoteSnapshot

log = logging.getLogger(__name__)

# Redis key for per-job health snapshots, scanned by the admin endpoint.
_HEALTH_KEY_PREFIX = "ingest:health:"
_HEALTH_TTL = 7 * 24 * 3600   # 7 days; cron runs at most weekly


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
        can `filter(None, ...)` instead of try/except per row."""
        ts_raw = row.get("time") or row.get("date")
        if not ts_raw:
            return None
        try:
            ts = date.fromisoformat(str(ts_raw)[:10])
        except ValueError:
            return None
        return cls(
            market=market,
            symbol=symbol,
            ts=ts,
            open=_to_float(row.get("open")),
            high=_to_float(row.get("high")),
            low=_to_float(row.get("low")),
            close=_to_float(row.get("close")),
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
    rows = [_bar_to_row(b) for b in bars]
    if not rows:
        return 0

    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    update_cols = {
        "open": None, "high": None, "low": None, "close": None,
        "volume": None, "source": None,
    }

    if dialect == "sqlite":
        stmt = sqlite_insert(OhlcvDaily).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["market", "symbol", "ts"],
            set_={k: getattr(stmt.excluded, k) for k in update_cols},
        )
    else:
        stmt = pg_insert(OhlcvDaily).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["market", "symbol", "ts"],
            set_={k: getattr(stmt.excluded, k) for k in update_cols},
        )

    await db.execute(stmt)
    await db.commit()
    return len(rows)


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


# ── Fundamentals snapshots ─────────────────────────────────────────

@dataclass(frozen=True)
class FundamentalsSnapshotRow:
    market: str
    symbol: str
    as_of: date
    pe_ratio: float | None
    pb_ratio: float | None
    dividend_yield: float | None
    eps: float | None
    revenue: float | None
    payload: dict[str, Any] | None
    source: str


async def upsert_fundamentals_snapshots(
    db: AsyncSession, rows: Iterable[FundamentalsSnapshotRow],
) -> int:
    """Bulk upsert. Returns number of input rows.

    Re-running same day overwrites the row's price columns + source.
    `ingested_at` is auto-refreshed via the column default on conflict.
    """
    payload = [
        {
            "market": r.market,
            "symbol": r.symbol,
            "as_of": r.as_of,
            "pe_ratio": r.pe_ratio,
            "pb_ratio": r.pb_ratio,
            "dividend_yield": r.dividend_yield,
            "eps": r.eps,
            "revenue": r.revenue,
            "payload": r.payload,
            "source": r.source,
        }
        for r in rows
    ]
    if not payload:
        return 0

    update_cols = (
        "pe_ratio", "pb_ratio", "dividend_yield", "eps", "revenue",
        "payload", "source",
    )
    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    if dialect == "sqlite":
        stmt = sqlite_insert(FundamentalsSnapshot).values(payload)
    else:
        stmt = pg_insert(FundamentalsSnapshot).values(payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=["market", "symbol", "as_of"],
        set_={k: getattr(stmt.excluded, k) for k in update_cols},
    )
    await db.execute(stmt)
    await db.commit()
    return len(payload)


async def read_latest_fundamentals(
    db: AsyncSession, market: str, symbol: str, *, max_age_days: int,
) -> dict[str, Any] | None:
    """Return the most recent snapshot if newer than `max_age_days`.

    Output shape mirrors `tw_market_service.get_fundamentals` (PE / PB /
    dividend_yield + symbol/market/exchange) so the caller can return it
    almost verbatim.
    """
    cutoff = date.today() - timedelta(days=max_age_days)
    stmt = (
        select(FundamentalsSnapshot)
        .where(
            FundamentalsSnapshot.market == market,
            FundamentalsSnapshot.symbol == symbol,
            FundamentalsSnapshot.as_of >= cutoff,
        )
        .order_by(FundamentalsSnapshot.as_of.desc())
        .limit(1)
    )
    row = await db.scalar(stmt)
    if row is None:
        return None
    return {
        "symbol":         row.symbol,
        "market":         row.market,
        "pe_ratio":       float(row.pe_ratio) if row.pe_ratio is not None else None,
        "pb_ratio":       float(row.pb_ratio) if row.pb_ratio is not None else None,
        "dividend_yield": float(row.dividend_yield) if row.dividend_yield is not None else None,
        "eps":            float(row.eps) if row.eps is not None else None,
        "revenue":        float(row.revenue) if row.revenue is not None else None,
        "as_of":          row.as_of.isoformat(),
        "data_source":    row.source,
    }


async def upsert_fundamentals_snapshots_autosession(
    rows: Iterable[FundamentalsSnapshotRow],
) -> int:
    """Open a fresh session and upsert. Errors are caught + logged so
    a Postgres outage in the read-path write-back never breaks the
    request."""
    rows = list(rows)
    if not rows:
        return 0
    try:
        async with AsyncSessionLocal() as db:
            return await upsert_fundamentals_snapshots(db, rows)
    except Exception as exc:
        log.warning("ingest.fundamentals.write_error",
                    extra={"market": rows[0].market, "count": len(rows), "error": str(exc)})
        return 0


async def read_latest_fundamentals_autosession(
    market: str, symbol: str, *, max_age_days: int,
) -> dict[str, Any] | None:
    """Same as `read_latest_fundamentals` but opens its own session and
    swallows DB errors so the read path falls through to upstream
    cleanly when Postgres is unhealthy."""
    try:
        async with AsyncSessionLocal() as db:
            return await read_latest_fundamentals(
                db, market, symbol, max_age_days=max_age_days,
            )
    except Exception as exc:
        log.warning("ingest.fundamentals.read_error",
                    extra={"market": market, "symbol": symbol, "error": str(exc)})
        return None


# ── News articles ──────────────────────────────────────────────────

# Strip whitespace + punctuation noise so the same article republished
# with trailing dots / smart quotes still hashes identically.
_NOISE_RE = re.compile(r"[\s　\.,;:!?。，！？“”‘’]+")


def _normalize_title(title: str) -> str:
    return _NOISE_RE.sub("", (title or "").lower())


def _canonical_link(link: str) -> str:
    """Strip query-string tracking params (utm_*, ref=...) so the same
    article shared via different campaigns deduplicates correctly."""
    if "?" not in link:
        return link.strip()
    base, _, qs = link.partition("?")
    keep = [
        kv for kv in qs.split("&")
        if kv and not kv.split("=", 1)[0].lower().startswith(("utm_", "ref", "fbclid", "gclid"))
    ]
    return base + (("?" + "&".join(keep)) if keep else "")


def compute_dedup_hash(title: str, link: str) -> str:
    raw = _normalize_title(title) + "|" + _canonical_link(link)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NewsArticleRow:
    market: str
    symbol: str | None
    published_at: datetime
    title: str
    link: str
    publisher: str | None
    summary: str | None
    payload: dict[str, Any] | None
    source: str

    @property
    def dedup_hash(self) -> str:
        return compute_dedup_hash(self.title, self.link)


async def insert_news_articles(
    db: AsyncSession, rows: Iterable[NewsArticleRow],
) -> int:
    """Bulk insert with on-conflict-do-nothing on `dedup_hash`. Returns
    the number of input rows passed (not the number actually inserted —
    duplicates are silently dropped at the DB layer)."""
    payload = [
        {
            "market": r.market,
            "symbol": r.symbol,
            "published_at": r.published_at,
            "title": r.title,
            "link": r.link,
            "publisher": r.publisher,
            "summary": r.summary,
            "payload": r.payload,
            "source": r.source,
            "dedup_hash": r.dedup_hash,
        }
        for r in rows
        if r.title and r.link
    ]
    if not payload:
        return 0

    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    if dialect == "sqlite":
        stmt = sqlite_insert(NewsArticle).values(payload)
    else:
        stmt = pg_insert(NewsArticle).values(payload)
    stmt = stmt.on_conflict_do_nothing(index_elements=["dedup_hash"])
    await db.execute(stmt)
    await db.commit()
    return len(payload)


async def read_recent_news(
    db: AsyncSession,
    market: str,
    *,
    symbol: str | None = None,
    limit: int = 20,
    max_age_days: int = 30,
) -> list[dict[str, Any]]:
    """Return the most recent articles for `(market, symbol)` newer than
    `max_age_days`. `symbol=None` matches market-wide articles
    (NULL symbol) only; pass an explicit symbol to fetch per-symbol news.

    Output shape mirrors `tw_market_service.get_news` so the caller can
    return the list verbatim.
    """
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    stmt = (
        select(NewsArticle)
        .where(NewsArticle.market == market, NewsArticle.published_at >= cutoff)
        .order_by(NewsArticle.published_at.desc())
        .limit(limit)
    )
    if symbol is None:
        stmt = stmt.where(NewsArticle.symbol.is_(None))
    else:
        stmt = stmt.where(NewsArticle.symbol == symbol)
    rows = (await db.scalars(stmt)).all()
    return [
        {
            "title":        r.title,
            "publisher":    r.publisher or "",
            "link":         r.link,
            "published_at": r.published_at.isoformat(),
            "thumbnail":    (r.payload or {}).get("thumbnail") if r.payload else None,
            "data_source":  r.source,
        }
        for r in rows
    ]


async def insert_news_articles_autosession(rows: Iterable[NewsArticleRow]) -> int:
    """Open own session + insert. Errors logged + swallowed."""
    rows = list(rows)
    if not rows:
        return 0
    try:
        async with AsyncSessionLocal() as db:
            return await insert_news_articles(db, rows)
    except Exception as exc:
        log.warning("ingest.news.write_error",
                    extra={"market": rows[0].market, "count": len(rows), "error": str(exc)})
        return 0


async def read_recent_news_autosession(
    market: str, *, symbol: str | None = None, limit: int = 20, max_age_days: int = 30,
) -> list[dict[str, Any]]:
    """Open own session + read. Errors logged; returns [] so the read
    path falls through cleanly to upstream."""
    try:
        async with AsyncSessionLocal() as db:
            return await read_recent_news(
                db, market, symbol=symbol, limit=limit, max_age_days=max_age_days,
            )
    except Exception as exc:
        log.warning("ingest.news.read_error",
                    extra={"market": market, "symbol": symbol, "error": str(exc)})
        return []


# ── Health snapshot ────────────────────────────────────────────────

@dataclass
class IngestHealth:
    job_id: str
    last_run_at: str | None
    ok: bool
    row_count: int
    error: str | None


async def record_health(
    job_id: str, *, ok: bool, row_count: int = 0, error: str | None = None,
) -> None:
    """Persist a per-job health snapshot in Redis.

    Stored as a single JSON blob keyed by job_id with a long TTL so the
    admin dashboard can reflect "last successful run" even after a quiet
    weekend. A separate Postgres table would be more durable but isn't
    needed yet — Redis state is regenerated on the next scheduled run.
    """
    payload = json.dumps({
        "job_id": job_id,
        "last_run_at": datetime.now(UTC).isoformat(),
        "ok": ok,
        "row_count": int(row_count),
        "error": error,
    })
    try:
        await cache_set(_HEALTH_KEY_PREFIX + job_id, payload, _HEALTH_TTL)
    except Exception as exc:
        log.warning("ingest.health.record_failed",
                    extra={"job_id": job_id, "error": str(exc)})


async def get_health(job_id: str) -> IngestHealth | None:
    raw = await cache_get(_HEALTH_KEY_PREFIX + job_id)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return IngestHealth(
        job_id=data.get("job_id", job_id),
        last_run_at=data.get("last_run_at"),
        ok=bool(data.get("ok", False)),
        row_count=int(data.get("row_count", 0)),
        error=data.get("error"),
    )


# ── Failure backoff (per-job) ──────────────────────────────────────
#
# When an ingest task fails repeatedly (e.g. FinMind down, token
# revoked) we want to back off from the upstream instead of hammering
# it every cycle. Two Redis keys per job:
#
#   ingest:failures:{job_id}    integer counter (TTL 7d so a weekend
#                               failure cluster ages out cleanly)
#   ingest:backoff:{job_id}     marker key with TTL == backoff window;
#                               while present, the task should skip
#
# Backoff schedule (exponential, hour-based, capped at 6h):
#   1 fail  → 1 h
#   2 fail  → 2 h
#   3 fail  → 4 h
#   4 fail+ → 6 h
#
# A successful run clears both keys via `clear_failures(job_id)`.

_FAILURE_KEY_PREFIX = "ingest:failures:"
_BACKOFF_KEY_PREFIX = "ingest:backoff:"
_FAILURE_TTL = 7 * 24 * 3600
_BACKOFF_MAX_SECONDS = 6 * 3600


def _backoff_seconds_for(failures: int) -> int:
    """Exponential 2^(N-1) hours, capped at 6 h. N=1 → 1h, N=4+ → 6h."""
    if failures < 1:
        return 0
    return min(3600 * (2 ** (failures - 1)), _BACKOFF_MAX_SECONDS)


async def record_failure(job_id: str) -> int:
    """Bump the failure counter and arm the backoff window. Returns the
    new failure count so callers can include it in their health string.
    Falls back to 1 (and skips backoff) if Redis is unreachable — in that
    case the next scheduled run will still try, mirroring the rest of the
    cache layer's "fall open on Redis outage" pattern."""
    try:
        new_count = await cache_incr(
            _FAILURE_KEY_PREFIX + job_id, ttl_seconds=_FAILURE_TTL,
        )
    except Exception as exc:
        log.warning("ingest.backoff.incr_failed",
                    extra={"job_id": job_id, "error": str(exc)})
        return 1
    backoff = _backoff_seconds_for(new_count)
    if backoff > 0:
        try:
            await cache_set(_BACKOFF_KEY_PREFIX + job_id, "1", backoff)
        except Exception as exc:
            log.warning("ingest.backoff.set_failed",
                        extra={"job_id": job_id, "error": str(exc)})
    return int(new_count)


async def clear_failures(job_id: str) -> None:
    """Reset failure counter + arm. Called on a successful run so a job
    that resumed working stops being throttled."""
    for key in (_FAILURE_KEY_PREFIX + job_id, _BACKOFF_KEY_PREFIX + job_id):
        try:
            await cache_delete(key)
        except Exception as exc:
            log.warning("ingest.backoff.clear_failed",
                        extra={"job_id": job_id, "key": key, "error": str(exc)})


async def backoff_remaining_seconds(job_id: str) -> int:
    """How many seconds until the backoff window expires. 0 means "go
    ahead and run". Uses Redis TTL on the marker key."""
    try:
        r = await get_redis()
        ttl = await r.ttl(_BACKOFF_KEY_PREFIX + job_id)
    except Exception as exc:
        log.warning("ingest.backoff.ttl_failed",
                    extra={"job_id": job_id, "error": str(exc)})
        return 0
    return max(0, int(ttl)) if ttl is not None else 0


async def get_failure_count(job_id: str) -> int:
    try:
        raw = await cache_get(_FAILURE_KEY_PREFIX + job_id)
    except Exception:
        return 0
    if not raw:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


async def list_health() -> list[IngestHealth]:
    """Scan Redis for every recorded job and return a stable-ordered list.

    Falls back to an empty list if Redis is unreachable so the admin
    endpoint stays available during cache outages.
    """
    try:
        r = await get_redis()
        keys = []
        async for key in r.scan_iter(match=_HEALTH_KEY_PREFIX + "*"):
            keys.append(key)
    except Exception as exc:
        log.warning("ingest.health.scan_failed", extra={"error": str(exc)})
        return []

    out: list[IngestHealth] = []
    for k in sorted(keys):
        job_id = k.removeprefix(_HEALTH_KEY_PREFIX) if isinstance(k, str) else k
        h = await get_health(job_id)
        if h is not None:
            out.append(h)
    return out


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
