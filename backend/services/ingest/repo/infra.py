"""Ingest infrastructure: health snapshot, failure backoff, sparkline aggregation."""
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from cache.redis_cache import cache_delete, cache_get, cache_incr, cache_set, get_redis
from db.session import AsyncSessionLocal

log = logging.getLogger(__name__)


# Redis key for per-job health snapshots, scanned by the admin endpoint.
_HEALTH_KEY_PREFIX = "ingest:health:"
_HEALTH_TTL = 7 * 24 * 3600   # 7 days; cron runs at most weekly


# ── Health snapshot ────────────────────────────────────────────────

@dataclass
class IngestHealth:
    job_id: str
    last_run_at: str | None
    ok: bool
    row_count: int
    error: str | None
    silent_deny: str | None = None
    latest_data_ts: str | None = None


def _classify_outcome(
    *, ok: bool, error: str | None, silent_deny: str | None,
) -> str:
    """Map a record_health call to a stable Prometheus outcome label.

    silent_deny wins over plain failure so operators can split paywall /
    quota events from real upstream errors. `skipped` and `queued` are
    fold together — both mean "we deliberately didn't try this run".
    """
    if ok:
        return "ok"
    if silent_deny is not None:
        return "silent_deny"
    err = (error or "").lower()
    if err.startswith("skipped") or err.startswith("queued"):
        return "skipped"
    return "failed"


async def record_health(
    job_id: str,
    *,
    ok: bool,
    row_count: int = 0,
    error: str | None = None,
    silent_deny: str | None = None,
    latest_data_ts: "date | datetime | None" = None,
    source: str | None = None,
) -> None:
    """Persist a per-job health snapshot in Redis + emit Prometheus.

    Stored as a single JSON blob keyed by job_id with a long TTL so the
    admin dashboard can reflect "last successful run" even after a quiet
    weekend. A separate Postgres table would be more durable but isn't
    needed yet — Redis state is regenerated on the next scheduled run.

    The Prometheus side is wired here (rather than per-task) so every
    existing caller picks up `ingest_runs_total` / `ingest_rows_*` for
    free; new caller-side kwargs (silent_deny, latest_data_ts, source)
    are all optional + keyword-only so the ~50 existing call sites
    keep working unchanged.
    """
    ts_iso: str | None = None
    if latest_data_ts is not None:
        if isinstance(latest_data_ts, datetime):
            ts_iso = latest_data_ts.isoformat()
        else:
            ts_iso = latest_data_ts.isoformat()

    payload = json.dumps({
        "job_id": job_id,
        "last_run_at": datetime.now(UTC).isoformat(),
        "ok": ok,
        "row_count": int(row_count),
        "error": error,
        "silent_deny": silent_deny,
        "latest_data_ts": ts_iso,
    })
    try:
        await cache_set(_HEALTH_KEY_PREFIX + job_id, payload, _HEALTH_TTL)
    except Exception as exc:
        log.warning("ingest.health.record_failed",
                    extra={"job_id": job_id, "error": str(exc)})

    outcome = _classify_outcome(
        ok=ok, error=error, silent_deny=silent_deny,
    )

    # Append-only history for the AdminPage 7-day sparkline. Best-
    # effort: a DB write failure here logs but doesn't break the cron
    # — the Redis snapshot above is the source of truth for the
    # "current" status row, history is purely cumulative.
    try:
        from models.ingest_health_history import IngestHealthHistory
        async with AsyncSessionLocal() as db:
            db.add(IngestHealthHistory(
                job_id=job_id,
                outcome=outcome,
                row_count=int(row_count),
            ))
            await db.commit()
    except Exception as exc:
        log.debug("ingest.health.history_append_failed",
                  extra={"job_id": job_id, "error": str(exc)})

    try:
        from middleware.metrics import (
            INGEST_DATA_FRESHNESS_SECONDS,
            INGEST_ROWS_WRITTEN_TOTAL,
            INGEST_RUNS_TOTAL,
            INGEST_SILENT_DENY_TOTAL,
        )
        INGEST_RUNS_TOTAL.labels(job_id=job_id, outcome=outcome).inc()
        if ok and row_count > 0:
            INGEST_ROWS_WRITTEN_TOTAL.labels(job_id=job_id).inc(row_count)
        if silent_deny is not None:
            INGEST_SILENT_DENY_TOTAL.labels(
                job_id=job_id, source=(source or "unknown"),
            ).inc()
        if latest_data_ts is not None:
            now_utc = datetime.now(UTC)
            if isinstance(latest_data_ts, datetime):
                ts_dt = latest_data_ts
                if ts_dt.tzinfo is None:
                    ts_dt = ts_dt.replace(tzinfo=UTC)
            else:
                # date → end-of-day UTC for an upper-bound freshness read
                ts_dt = datetime.combine(
                    latest_data_ts, datetime.min.time(), tzinfo=UTC,
                )
            age = max(0.0, (now_utc - ts_dt).total_seconds())
            INGEST_DATA_FRESHNESS_SECONDS.labels(job_id=job_id).set(age)
    except Exception as exc:
        # Metrics are best-effort: if the prometheus_client registry
        # isn't importable in a test fixture, don't break the task.
        log.debug("ingest.health.metrics_failed",
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
        silent_deny=data.get("silent_deny"),
        latest_data_ts=data.get("latest_data_ts"),
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


# ── Sparkline aggregation ─────────────────────────────────────────


@dataclass
class IngestHealthHistoryDay:
    """One day's outcome roll-up for a single job. Days with zero
    runs are omitted from the result list — the frontend treats a
    gap as "no data" automatically."""
    date: str          # ISO date in UTC
    ok: int = 0
    silent_deny: int = 0
    failed: int = 0
    skipped: int = 0


async def get_health_history(
    job_id: str, *, days: int = 7,
) -> list[IngestHealthHistoryDay]:
    """Daily outcome counts for `job_id` over the last `days` UTC
    calendar days, oldest-first.

    Days with zero runs are omitted — the UI's sparkline renders a
    gray "no data" cell for any missing date in the window. The
    aggregation runs entirely in SQL (`GROUP BY date(recorded_at)`)
    so a 7-day query stays a single index range scan + group-by.

    Returns an empty list (rather than raising) when the DB is
    unreachable, mirroring `list_health`'s fail-soft behaviour so
    the admin endpoint doesn't 500 on a transient blip.
    """
    if days <= 0:
        return []
    cutoff = datetime.now(UTC) - timedelta(days=days)
    try:
        async with AsyncSessionLocal() as db:
            from models.ingest_health_history import IngestHealthHistory
            day_col = func.date(IngestHealthHistory.recorded_at)
            stmt = (
                select(
                    day_col.label("d"),
                    IngestHealthHistory.outcome,
                    func.count().label("n"),
                )
                .where(
                    IngestHealthHistory.job_id == job_id,
                    IngestHealthHistory.recorded_at >= cutoff,
                )
                .group_by(day_col, IngestHealthHistory.outcome)
                .order_by(day_col)
            )
            rows = (await db.execute(stmt)).all()
    except Exception as exc:
        log.warning(
            "ingest.health.history_query_failed",
            extra={"job_id": job_id, "error": str(exc)},
        )
        return []

    by_day: dict[str, IngestHealthHistoryDay] = {}
    for d, outcome, n in rows:
        # `func.date(...)` returns a `date` on Postgres and a string on
        # SQLite; normalize so the API always emits ISO-string dates.
        day_str = d.isoformat() if hasattr(d, "isoformat") else str(d)
        bucket = by_day.setdefault(day_str, IngestHealthHistoryDay(date=day_str))
        if outcome == "ok":
            bucket.ok = int(n)
        elif outcome == "silent_deny":
            bucket.silent_deny = int(n)
        elif outcome == "failed":
            bucket.failed = int(n)
        elif outcome == "skipped":
            bucket.skipped = int(n)
    return [by_day[k] for k in sorted(by_day.keys())]
