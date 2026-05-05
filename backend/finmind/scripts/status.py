"""Health report for the FinMind clone subsystem.

Usage (from `backend/`):

    python -m finmind.scripts.status            # one-shot human-readable
    python -m finmind.scripts.status --watch    # re-render every 30s
    python -m finmind.scripts.status --json     # machine-readable

Read-only — opens a session against `FINMIND_DATABASE_URL` and queries
the routing + ledger tables. Designed so a developer running a backfill
in one terminal can `--watch` in another and see chunks completing in
real time.

The data-collection layer (`collect_status`) is split out from the
rendering so `finmind/scripts/check.py` can reuse it for cron / k8s
liveness probes without duplicating SQL.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

# When run as `python -m finmind.scripts.status` from `backend/`, the
# package import works. When invoked from a different cwd, inject
# backend/ on sys.path defensively.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from finmind.dataset_catalog import all_entries  # noqa: E402
from finmind.db.session import (  # noqa: E402
    FinmindAsyncSessionLocal,
    finmind_engine,
)
from finmind.models.backfill_progress import BackfillProgress  # noqa: E402
from finmind.models.dataset_source import DatasetSource  # noqa: E402


# ── Stuck-running threshold ──────────────────────────────────────
# A backfill chunk left in `status='running'` longer than this is
# almost certainly an orphaned ingest worker (process killed before
# it could mark done/failed). Surfaced separately so an operator can
# decide whether to reset to 'pending' or investigate.
STUCK_RUNNING_AFTER = timedelta(hours=1)

RECENT_ERRORS_WINDOW = timedelta(hours=24)
MAX_RECENT_ERRORS = 10


@dataclass
class StatusReport:
    """Structured snapshot of the FinMind subsystem state. Both renderers
    (`status.py` human/json) and `check.py` (exit-code) consume this."""

    alembic: dict[str, Any] = field(default_factory=dict)
    catalog: dict[str, Any] = field(default_factory=dict)
    phase1_coverage: dict[str, dict[str, int]] = field(default_factory=dict)
    active_ingestion: dict[str, Any] = field(default_factory=dict)
    backfill: dict[str, int] = field(default_factory=dict)
    recent_errors: list[dict[str, Any]] = field(default_factory=list)
    quota: dict[str, Any] | None = None
    disk: dict[str, int] | None = None
    generated_at: str = ""


# ── Collectors ──────────────────────────────────────────────────

async def _collect_alembic(session: AsyncSession) -> dict[str, Any]:
    """Read the version table directly. Avoids importing alembic at runtime
    for fast-path queries; `init_db --check` does the full alembic walk."""
    try:
        result = await session.execute(
            text("SELECT version_num FROM alembic_version_finmind LIMIT 1")
        )
        current = result.scalar_one_or_none()
    except Exception:
        # Table doesn't exist yet → init_db hasn't run.
        return {"at_head": False, "current": None, "expected": "0002"}

    expected = "0011"  # bump when adding migrations
    return {
        "at_head": current == expected,
        "current": current,
        "expected": expected,
    }


async def _collect_catalog(session: AsyncSession) -> dict[str, Any]:
    expected = len(list(all_entries()))
    seeded = (
        await session.execute(
            select(func.count()).select_from(DatasetSource)
        )
    ).scalar_one()
    return {
        "seeded": int(seeded),
        "expected": expected,
        "ok": int(seeded) == expected,
    }


async def _collect_phase1_coverage(
    session: AsyncSession,
) -> dict[str, dict[str, int]]:
    """Per-category: how many datasets have a non-empty `local_table`
    (= their destination table has been migrated in)."""
    rows = (
        await session.execute(
            select(
                DatasetSource.category,
                func.count().label("total"),
                func.sum(
                    case(
                        (DatasetSource.local_table != "", 1),
                        else_=0,
                    )
                ).label("built"),
            ).group_by(DatasetSource.category)
        )
    ).all()
    return {
        category: {"built": int(built or 0), "total": int(total or 0)}
        for category, total, built in rows
    }


async def _collect_active_ingestion(session: AsyncSession) -> dict[str, Any]:
    enabled = (
        await session.execute(
            select(func.count())
            .select_from(DatasetSource)
            .where(DatasetSource.enabled.is_(True))
        )
    ).scalar_one()

    by_cat = (
        await session.execute(
            select(DatasetSource.category, func.count())
            .where(DatasetSource.enabled.is_(True))
            .group_by(DatasetSource.category)
        )
    ).all()
    return {
        "enabled": int(enabled),
        "by_category": {cat: int(c) for cat, c in by_cat},
    }


async def _collect_backfill(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(BackfillProgress.status, func.count())
            .group_by(BackfillProgress.status)
        )
    ).all()
    counts = {
        "pending": 0,
        "running": 0,
        "done": 0,
        "failed": 0,
        "skipped": 0,
    }
    for status_val, c in rows:
        counts[status_val] = int(c)

    # Stuck-running: alive longer than the threshold without finishing.
    cutoff = datetime.now(tz=timezone.utc) - STUCK_RUNNING_AFTER
    stuck = (
        await session.execute(
            select(func.count())
            .select_from(BackfillProgress)
            .where(
                and_(
                    BackfillProgress.status == "running",
                    BackfillProgress.started_at < cutoff,
                )
            )
        )
    ).scalar_one()
    counts["stuck_running"] = int(stuck)
    return counts


async def _collect_recent_errors(
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Two error sources merged: `dataset_sources.last_error` (most recent
    daily-ingest failure per dataset) and `backfill_progress` rows in
    status='failed' (per-chunk historical-ingest failures). Capped at
    MAX_RECENT_ERRORS rows total, newest first."""
    cutoff = datetime.now(tz=timezone.utc) - RECENT_ERRORS_WINDOW
    out: list[dict[str, Any]] = []

    ds_rows = (
        await session.execute(
            select(
                DatasetSource.dataset_code,
                DatasetSource.last_error,
                DatasetSource.last_ingest_at,
            )
            .where(DatasetSource.last_error.is_not(None))
            .order_by(DatasetSource.last_ingest_at.desc().nulls_last())
            .limit(MAX_RECENT_ERRORS)
        )
    ).all()
    for code, err, ts in ds_rows:
        out.append(
            {
                "source": "dataset_sources",
                "dataset_code": code,
                "error": err,
                "ts": ts.isoformat() if ts else None,
            }
        )

    bf_rows = (
        await session.execute(
            select(
                BackfillProgress.dataset_code,
                BackfillProgress.symbol,
                BackfillProgress.error_message,
                BackfillProgress.finished_at,
            )
            .where(
                and_(
                    BackfillProgress.status == "failed",
                    BackfillProgress.finished_at >= cutoff,
                )
            )
            .order_by(BackfillProgress.finished_at.desc())
            .limit(MAX_RECENT_ERRORS)
        )
    ).all()
    for code, sym, err, ts in bf_rows:
        out.append(
            {
                "source": "backfill_progress",
                "dataset_code": code,
                "symbol": sym,
                "error": err,
                "ts": ts.isoformat() if ts else None,
            }
        )

    out.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return out[:MAX_RECENT_ERRORS]


async def _collect_quota() -> dict[str, Any] | None:
    """Read FinMind hourly quota counter + quota-exhausted alert
    counter from Redis, if available. Returns None on any failure —
    Redis is optional infrastructure for this report (nice-to-have,
    not load-bearing)."""
    try:
        from cache.redis_cache import (
            key_finmind_counter,
            key_finmind_quota_exhausted_counter,
        )
        from config import settings as main_settings

        # Late import — only depended on when Redis is actually reachable.
        import redis.asyncio as aioredis

        client = aioredis.from_url(main_settings.REDIS_URL)
        try:
            raw_used = await client.get(key_finmind_counter())
            raw_exhausted = await client.get(
                key_finmind_quota_exhausted_counter()
            )
        finally:
            await client.aclose()

        used = int(raw_used) if raw_used else 0
        exhausted = int(raw_exhausted) if raw_exhausted else 0
        from finmind.config import finmind_settings

        limit = finmind_settings.FINMIND_HOURLY_REQUEST_LIMIT
        return {
            "available": True,
            "used": used,
            "limit": limit,
            "ratio": used / limit if limit else 0.0,
            # Number of overrun events in the rolling hour window — bumped
            # by `_query` whenever a call hits the cap (both in strict
            # mode where it raises and quiet mode where it returns []).
            # A non-zero value usually means parallel backfills are
            # saturating the cap; consider serializing or lowering
            # FINMIND_HOURLY_REQUEST_LIMIT to leave headroom.
            "exhausted_1h": exhausted,
        }
    except Exception:
        return None


async def _collect_disk(session: AsyncSession) -> dict[str, int] | None:
    """Per-table size in bytes. Postgres-only — uses
    `pg_total_relation_size`. SQLite (tests) returns None."""
    if session.bind.dialect.name != "postgresql":
        return None

    rows = (
        await session.execute(
            select(DatasetSource.local_table).where(
                DatasetSource.local_table != ""
            )
        )
    ).all()
    tables = {r[0] for r in rows}
    tables.update({"dataset_sources", "backfill_progress"})

    out: dict[str, int] = {}
    for tbl in sorted(tables):
        try:
            size = (
                await session.execute(
                    text(
                        "SELECT pg_total_relation_size(:tbl::regclass)"
                    ).bindparams(tbl=tbl)
                )
            ).scalar_one()
            out[tbl] = int(size or 0)
        except Exception:
            # Table referenced in dataset_sources.local_table but not yet
            # migrated — skip silently rather than poisoning the report.
            continue
    return out


async def collect_status(session: AsyncSession) -> StatusReport:
    """Single entry point — runs each collector serially.

    Serial because a single AsyncSession can't service concurrent
    queries ("session is provisioning a new connection" error). The
    full collection is small (~7 light queries), so the wall-clock
    cost is well under 100 ms even on PG; not worth the complexity
    of a session-per-collector pool.
    """
    alembic = await _collect_alembic(session)
    catalog = await _collect_catalog(session)
    phase1 = await _collect_phase1_coverage(session)
    active = await _collect_active_ingestion(session)
    backfill = await _collect_backfill(session)
    errors = await _collect_recent_errors(session)
    disk = await _collect_disk(session)
    quota = await _collect_quota()  # outside session — uses Redis

    return StatusReport(
        alembic=alembic,
        catalog=catalog,
        phase1_coverage=phase1,
        active_ingestion=active,
        backfill=backfill,
        recent_errors=errors,
        quota=quota,
        disk=disk,
        generated_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    )


# ── Renderers ──────────────────────────────────────────────────

def _bar(filled: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "░" * width
    pct = filled / total
    n = int(round(pct * width))
    return "█" * n + "░" * (width - n)


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def render_human(report: StatusReport) -> str:
    lines: list[str] = []
    lines.append(
        f"═══ FinMind Clone — Health Report ({report.generated_at}) ═══"
    )
    lines.append("")

    # Alembic
    a = report.alembic
    if a["at_head"]:
        lines.append(f"Migrations: ✓ at head ({a['current']})")
    elif a["current"] is None:
        lines.append("Migrations: ✗ NOT INITIALIZED — run init_db")
    else:
        lines.append(
            f"Migrations: ✗ at {a['current']}, expected {a['expected']}"
        )

    # Catalog
    c = report.catalog
    mark = "✓" if c["ok"] else "✗"
    lines.append(
        f"Catalog:    {mark} {c['seeded']}/{c['expected']} datasets seeded"
    )
    lines.append("")

    # Phase 1 coverage
    lines.append("Phase 1 (schema coverage):")
    for cat in sorted(report.phase1_coverage):
        s = report.phase1_coverage[cat]
        pct = (
            int(round(s["built"] / s["total"] * 100))
            if s["total"]
            else 0
        )
        lines.append(
            f"  {cat:18s}: {s['built']:2d}/{s['total']:2d}  "
            f"{_bar(s['built'], s['total'])}  {pct:3d}%"
        )
    lines.append("")

    # Active ingestion
    ai = report.active_ingestion
    lines.append(f"Active ingestion: {ai['enabled']} datasets enabled")
    if ai["by_category"]:
        for cat, n in sorted(ai["by_category"].items()):
            lines.append(f"  {cat}: {n}")
    lines.append("")

    # Backfill
    bf = report.backfill
    total = sum(bf.values()) - bf.get("stuck_running", 0)
    if total == 0:
        lines.append("Backfill progress: (no chunks scheduled)")
    else:
        lines.append("Backfill progress:")
        for status_val in ("pending", "running", "done", "failed", "skipped"):
            n = bf.get(status_val, 0)
            lines.append(f"  {status_val:10s}: {n:6d}")
        if bf.get("stuck_running", 0):
            lines.append(
                f"  ⚠  stuck > {STUCK_RUNNING_AFTER}: "
                f"{bf['stuck_running']}"
            )
    lines.append("")

    # Recent errors
    if not report.recent_errors:
        lines.append("Recent errors (last 24h): (none)")
    else:
        lines.append(f"Recent errors (last 24h, top {len(report.recent_errors)}):")
        for e in report.recent_errors:
            sym = f" {e.get('symbol')}" if e.get("symbol") else ""
            lines.append(
                f"  [{e['source']}] {e['dataset_code']}{sym} @ "
                f"{e.get('ts') or '-'}"
            )
            err = (e.get("error") or "").splitlines()[0][:120]
            lines.append(f"      {err}")
    lines.append("")

    # Quota
    if report.quota is None:
        lines.append("FinMind quota:    (Redis unavailable — skipped)")
    else:
        q = report.quota
        warn = " ⚠" if q["ratio"] >= 0.9 else ""
        lines.append(
            f"FinMind quota:    {q['used']}/{q['limit']} this hour "
            f"({q['ratio'] * 100:.1f}%){warn}"
        )
        # Surface the overrun-event count so saturated parallel backfills
        # are obvious before chunks pile up as failed in the ledger.
        # Zero in the steady state; non-zero is a hint to lower the
        # local limit or serialize the workload.
        ex = q.get("exhausted_1h", 0)
        if ex:
            lines.append(
                f"  ⚠ quota overrun events: {ex} in the last hour "
                f"(reduce parallel backfills or lower "
                f"FINMIND_HOURLY_REQUEST_LIMIT)"
            )

    # Disk
    if report.disk is None:
        lines.append("DB size:          (SQLite — pg_total_relation_size N/A)")
    elif not report.disk:
        lines.append("DB size:          (no destination tables yet)")
    else:
        total_bytes = sum(report.disk.values())
        lines.append(f"DB size:          {_fmt_bytes(total_bytes)} total")
        for tbl, size in sorted(
            report.disk.items(), key=lambda kv: -kv[1]
        )[:10]:
            lines.append(f"  {tbl:32s} {_fmt_bytes(size)}")

    return "\n".join(lines)


def render_json(report: StatusReport) -> str:
    return json.dumps(asdict(report), default=str, indent=2, ensure_ascii=False)


# ── CLI ────────────────────────────────────────────────────────

async def _run_once(as_json: bool) -> None:
    async with FinmindAsyncSessionLocal() as session:
        report = await collect_status(session)
    print(render_json(report) if as_json else render_human(report))


async def _run_watch(interval: int, as_json: bool) -> None:
    """Re-render every `interval` seconds. Ctrl-C exits cleanly."""
    stop = asyncio.Event()

    def _signal_handler():
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass  # Windows.

    while not stop.is_set():
        # ANSI clear screen + cursor home — works on every modern terminal.
        if not as_json:
            sys.stdout.write("\033[2J\033[H")
        await _run_once(as_json)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Re-render every --interval seconds until Ctrl-C.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Watch refresh interval in seconds (default: 30).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a single JSON object instead of human-readable text.",
    )
    args = parser.parse_args()

    if args.watch:
        await _run_watch(args.interval, args.json)
    else:
        await _run_once(args.json)

    await finmind_engine.dispose()
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    os.chdir(_BACKEND_DIR)
    main()
