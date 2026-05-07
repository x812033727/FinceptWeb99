"""Daily strategy health monitoring (PR-4b).

Once per UTC day (02:00) iterate every non-stale strategy template,
compute its rolling-30 metrics via `strategy_health_service`, and
persist a snapshot. Any snapshot with non-empty `status_flags`
fires an admin notification through the existing
`notification_service` so the operator sees drift / collapse
signals without watching dashboards.

Multi-pod safe via the same Redis SET-NX lock pattern as
`score_news_sentiment` — without it, every pod would race to write
the same `(strategy_id, snapshot_date)` and one pod's PK conflict
would log spurious errors. With the lock, exactly one pod runs the
sweep per cron tick.

Runs cheap: a few aggregate queries per strategy + one upsert. A
deployment with ~100 strategies finishes in seconds.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from models.discussion_strategy_template import DiscussionStrategyTemplate
from services import strategy_health_service as hsvc
from services import strategy_maturity_service as msvc
from services.ingest.repository import record_health
from services.notification_service import notify_user

log = logging.getLogger(__name__)

JOB_ID = "monitor_strategy_health"
_LOCK_KEY = "lock:monitor_strategy_health"
_LOCK_TTL = 30 * 60   # 30 min — comfortable for ~100 strategies


async def _alert_owner(
    *,
    owner_id: UUID,
    strategy_id: UUID,
    strategy_name: str,
    flags: list[str],
) -> None:
    """Push one notification per (owner, strategy, flag-set). Best-
    effort: a notification dispatch failure shouldn't block the
    snapshot writes for the rest of the cohort."""
    if not flags:
        return
    try:
        await notify_user(
            str(owner_id),
            {
                "kind": "strategy_health_alert",
                "strategy_id": str(strategy_id),
                "strategy_name": strategy_name,
                "status_flags": flags,
            },
        )
    except Exception as exc:
        log.warning(
            "monitor_strategy_health.alert_failed",
            extra={
                "strategy_id": str(strategy_id),
                "owner_id": str(owner_id),
                "error": str(exc),
            },
        )


async def run_health_monitor() -> dict:
    """Returns a counters dict for the IngestHealthCard."""
    counters = {
        "strategies_total": 0,
        "snapshots_written": 0,
        "alerts_fired": 0,
        "errors": 0,
    }

    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("monitor_strategy_health.skipped (lock held)")
        return {**counters, "skipped": "lock_held"}

    try:
        async with AsyncSessionLocal() as db:
            strategies = list((await db.scalars(
                select(DiscussionStrategyTemplate).where(
                    DiscussionStrategyTemplate.deleted_at.is_(None),
                )
            )).all())
        counters["strategies_total"] = len(strategies)

        for tmpl in strategies:
            # Stale strategies skip the snapshot — they would just
            # produce all-NULL rows and bloat the table. The
            # maturity check below still updates the tier so a
            # stale row doesn't get stuck mid-flag.
            try:
                async with AsyncSessionLocal() as db:
                    tier, _ = await msvc.update_maturity_tier(
                        db, strategy_id=tmpl.id,
                    )
                if tier == "stale":
                    continue

                async with AsyncSessionLocal() as db:
                    row = await hsvc.record_snapshot(
                        db, strategy_id=tmpl.id,
                    )
                counters["snapshots_written"] += 1

                flags = list(row.status_flags or [])
                if flags:
                    await _alert_owner(
                        owner_id=tmpl.owner_id,
                        strategy_id=tmpl.id,
                        strategy_name=tmpl.name,
                        flags=flags,
                    )
                    counters["alerts_fired"] += 1
            except Exception as exc:
                counters["errors"] += 1
                log.warning(
                    "monitor_strategy_health.strategy_failed",
                    extra={
                        "strategy_id": str(tmpl.id),
                        "error": str(exc),
                    },
                )
    finally:
        await release_lock(_LOCK_KEY)

    return counters


async def health_monitor_job() -> None:
    """APScheduler entry — wraps `run_health_monitor` with the
    standard health-row recording + structured logging surface so
    the IngestHealthCard sees the same shape as every other
    background job."""
    started = datetime.now(UTC)
    try:
        result = await run_health_monitor()
        ok = result.get("errors", 0) == 0
        message = (
            f"strategies={result['strategies_total']} "
            f"snapshots={result['snapshots_written']} "
            f"alerts={result['alerts_fired']} "
            f"errors={result['errors']}"
        )
        if result.get("skipped"):
            message = f"skipped (reason: {result['skipped']})"
        await record_health(
            JOB_ID, ok=ok, row_count=result["snapshots_written"],
            error=None if ok else message,
        )
        log.info(
            "monitor_strategy_health.complete",
            extra={
                "duration_s": (
                    datetime.now(UTC) - started
                ).total_seconds(),
                **result,
            },
        )
    except Exception as exc:
        log.exception("monitor_strategy_health.failed")
        try:
            await record_health(JOB_ID, ok=False, error=str(exc))
        except Exception:
            pass
