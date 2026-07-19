from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends

from auth.permissions import require_admin
from services.ingest import repository as ingest_repo
from services.ingest_schedules import (
    FINMIND_TW_MARKETWIDE_JOB_ID,
    is_finmind_tw_marketwide_run_stale,
)

from ..schemas import (
    IngestHealthHistoryDayOut,
    IngestHealthHistoryOut,
    IngestHealthOut,
    SchedulerHeartbeatOut,
)

router = APIRouter()
AdminUser = Annotated[dict, Depends(require_admin)]


# ── Scheduled ingest health ──────────────────────────────────────

@router.get("/ingest/health", response_model=list[IngestHealthOut])
async def ingest_health(_: AdminUser) -> list[IngestHealthOut]:
    """Per-job snapshot for every scheduled ingest task.

    State lives in Redis with a 7-day TTL; entries disappear after a
    week of silence so a removed job doesn't linger forever.
    """
    from services.freshness import is_data_stale

    now = datetime.now(UTC)

    return [
        IngestHealthOut(
            job_id=h.job_id,
            last_run_at=h.last_run_at,
            ok=h.ok,
            row_count=h.row_count,
            error=h.error,
            silent_deny=h.silent_deny,
            latest_data_ts=h.latest_data_ts,
            data_stale=is_data_stale(
                last_run_at=h.last_run_at,
                latest_data_ts=h.latest_data_ts,
            ),
            run_stale=(
                h.job_id == FINMIND_TW_MARKETWIDE_JOB_ID
                and is_finmind_tw_marketwide_run_stale(
                    h.last_run_at,
                    now=now,
                )
            ),
        )
        for h in await ingest_repo.list_health()
    ]


@router.get("/scheduler/health", response_model=SchedulerHeartbeatOut)
async def scheduler_heartbeat(_: AdminUser) -> SchedulerHeartbeatOut:
    """APScheduler liveness probe — has the scheduler process beat
    its 30s-cadence heartbeat recently?

    Distinct from `/ingest/health` because per-job freshness can't
    distinguish "job legitimately throttled" from "scheduler dead".
    A missing heartbeat signals the latter unambiguously.
    """
    from services.scheduler_health import read_heartbeat

    snap = await read_heartbeat()
    return SchedulerHeartbeatOut(
        last_beat_at=snap.last_beat_at,
        age_seconds=snap.age_seconds,
        stale=snap.stale,
        version=snap.version,
        ttl_seconds=snap.ttl_seconds,
    )


@router.get(
    "/ingest/{job_id}/history",
    response_model=IngestHealthHistoryOut,
)
async def ingest_health_history(
    job_id: str, _: AdminUser, days: int = 7,
) -> IngestHealthHistoryOut:
    """Daily outcome roll-up for `job_id` over the last `days` UTC
    calendar days. Powers the IngestHealthCard's per-row sparkline.

    `days` is clamped to [1, 30] to bound the worst-case query — the
    frontend only renders 7 cells anyway, but the parameter stays
    flexible for ad-hoc operator queries via curl.
    """
    bounded = max(1, min(30, days))
    rows = await ingest_repo.get_health_history(job_id, days=bounded)
    return IngestHealthHistoryOut(
        job_id=job_id,
        days=bounded,
        history=[
            IngestHealthHistoryDayOut(
                date=r.date,
                ok=r.ok,
                silent_deny=r.silent_deny,
                failed=r.failed,
                skipped=r.skipped,
            )
            for r in rows
        ],
    )
