from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from api.system.router import VersionStatus
from auth.permissions import require_admin
from services.ingest import repository as ingest_repo
from services.version_service import force_refresh_status, trigger_update

from .schemas import (
    IngestRetryResult,
    UpdateResult,
)

router = APIRouter()
AdminUser = Annotated[dict, Depends(require_admin)]

# AdminPage proxy to the FinMind clone subsystem — separate sub-router
# under /api/admin/finmind so the React app uses its existing JWT
# admin auth instead of needing a separate X-Finmind-Admin-Key flow.
# See finmind_proxy.py for rationale on the architectural boundary.
from api.admin.finmind_proxy import router as _finmind_proxy_router  # noqa: E402

router.include_router(
    _finmind_proxy_router, prefix="/finmind", tags=["AdminFinMind"],
)

# Read-only DB browser for the AdminPage「DB」tab — see db_browser.py
# for the security model (schema allowlist, catalog-verified
# identifiers, masked credential columns).
from api.admin.db_browser import router as _db_browser_router  # noqa: E402

router.include_router(
    _db_browser_router, prefix="/db", tags=["AdminDbBrowser"],
)

# FinMind 全量回填 chain control + monitor — backed by an in-process
# asyncio task in services/finmind_chain_service.py. Replaces the
# host-level finmind_chain.sh (which stays as a redeploy fallback).
from api.admin.finmind_chain import router as _finmind_chain_router  # noqa: E402

router.include_router(
    _finmind_chain_router, prefix="/finmind", tags=["AdminFinMindChain"],
)

# Domain endpoints split into per-banner sub-modules under
# router_parts/. Each part defines its own `router = APIRouter()` and
# is mounted here with no prefix, so the effective route set is
# identical to when every endpoint hung directly off this router.
# The endpoints that stay in this facade (system update / version
# check / ingest retry + dispatcher) do so because the test-suite
# patches their collaborators via `api.admin.router.<name>`, which
# only resolves if the endpoint's module globals ARE this module.
from api.admin.router_parts import (  # noqa: E402
    deploy,
    ingest_health,
    lessons,
    llm_keys,
    market_keys,
    personas,
    post_mortem,
    runtime_settings,
    signal_audit,
    signal_quality,
    stats,
    system_tasks,
    usage,
    users,
)

router.include_router(stats.router)
router.include_router(users.router)
router.include_router(deploy.router)
router.include_router(llm_keys.router)
router.include_router(personas.router)
router.include_router(system_tasks.router)
router.include_router(runtime_settings.router)
router.include_router(usage.router)
router.include_router(ingest_health.router)
router.include_router(market_keys.router)
router.include_router(signal_audit.router)
router.include_router(post_mortem.router)
router.include_router(signal_quality.router)
router.include_router(lessons.router)

RETRYABLE_INGEST_JOBS = {
    "ingest_news_tw": "Taiwan news ingest",
    "ingest_news_international": "International news ingest",
    "ingest_news_feeds": "TW direct-publisher RSS ingest (cnyes/udn/ltn/cna)",
    "enrich_news_fulltext": "News full-text extraction (direct-feed bodies)",
    "ingest_ohlcv_tw": "TW daily OHLCV ingest (TWSE → FinMind)",
    "ingest_institutional_tw": "TW institutional flow",
    "ingest_margin_tw": "TW margin balance",
    "ingest_revenue_tw": "TW monthly revenue (FinMind, paywalled)",
    "ingest_revenue_tw_slow": "TW monthly revenue (MOPS slow per-symbol)",
    "ingest_buyback_tw": "TW buyback announcements",
    "ingest_govt_bank_flow_tw": "TW eight-bank daily flow",
    "ingest_stock_futures_oi_tw": "TW 個股期貨 三大法人未平倉 (FinMind sponsor)",
    "ingest_tw_vix": "TW VIX 臺指選擇權波動率指數 (TAIFEX CSV)",
    "ingest_taiex_history": "TAIEX daily history",
    "ingest_taiex_tr_history": "TAIEX TR (total return) history",
    "ingest_risk_signals_tw": "TW risk signals (disposition / suspended / day-trading)",
    "ingest_holdings_aggregates_tw": "TW holdings + market-institutional aggregates",
    "score_discussion_outcomes": "Discussion D1-D5 scoreboard",
    "score_news_sentiment": "News sentiment scoring (LLM)",
    "snapshot_signal_audit": "Signal-audit daily snapshot (sparkline source)",
}


@router.post("/update", response_model=UpdateResult)
async def trigger_system_update(_: AdminUser) -> UpdateResult:
    result = await trigger_update()
    return UpdateResult(**result)


@router.post("/version/check", response_model=VersionStatus)
async def check_for_updates(_: AdminUser) -> VersionStatus:
    """Force a fresh GitHub lookup, bypassing the Redis cache.

    Same payload shape as `GET /api/system/version` so the frontend can drop
    the response straight into its `["version"]` query cache.
    """
    data = await force_refresh_status()
    return VersionStatus(**data)


async def _run_ingest_job_once(job_id: str) -> None:
    """Dispatch a manual run for one whitelisted scheduled job.

    Each entry in `RETRYABLE_INGEST_JOBS` must have a corresponding
    branch here. The runner imports the task module lazily so a
    manual-retry path that's never hit doesn't pay the import cost
    on every admin request.
    """
    if job_id == "ingest_news_tw":
        from tasks.ingest_news_tw import run
        await run()
    elif job_id == "ingest_news_international":
        from tasks.ingest_news_international import run
        await run()
    elif job_id == "ingest_news_feeds":
        from tasks.ingest_news_feeds import run
        await run()
    elif job_id == "enrich_news_fulltext":
        from tasks.enrich_news_fulltext import run
        await run()
    elif job_id == "ingest_ohlcv_tw":
        from tasks.ingest_ohlcv_tw import run
        await run()
    elif job_id == "ingest_institutional_tw":
        from tasks.ingest_institutional_tw import run
        await run()
    elif job_id == "ingest_margin_tw":
        from tasks.ingest_margin_tw import run
        await run()
    elif job_id == "ingest_revenue_tw":
        from tasks.ingest_revenue_tw import run
        await run()
    elif job_id == "ingest_revenue_tw_slow":
        from tasks.ingest_revenue_tw_slow import run
        await run()
    elif job_id == "ingest_buyback_tw":
        from tasks.ingest_buyback_tw import run
        await run()
    elif job_id == "ingest_govt_bank_flow_tw":
        from tasks.ingest_govt_bank_flow_tw import run
        await run()
    elif job_id == "ingest_stock_futures_oi_tw":
        from tasks.ingest_stock_futures_oi_tw import run
        await run()
    elif job_id == "ingest_tw_vix":
        from tasks.ingest_tw_vix import run
        await run()
    elif job_id == "ingest_taiex_history":
        from tasks.ingest_taiex_history import run
        await run()
    elif job_id == "ingest_taiex_tr_history":
        from tasks.ingest_taiex_tr_history import run
        await run()
    elif job_id == "ingest_risk_signals_tw":
        from tasks.ingest_risk_signals_tw import run
        await run()
    elif job_id == "ingest_holdings_aggregates_tw":
        from tasks.ingest_holdings_aggregates_tw import run
        await run()
    elif job_id == "score_discussion_outcomes":
        from tasks.score_discussion_outcomes import run
        await run()
    elif job_id == "score_news_sentiment":
        from tasks.score_news_sentiment import run
        await run()
    elif job_id == "snapshot_signal_audit":
        from tasks.snapshot_signal_audit import run
        await run()


@router.post("/ingest/{job_id}/retry", response_model=IngestRetryResult)
async def retry_ingest_job(
    job_id: str, background_tasks: BackgroundTasks, _: AdminUser,
) -> IngestRetryResult:
    """Clear a job's Redis backoff and queue one immediate run.

    The scheduled job still owns locking and health recording. This endpoint
    only lets an operator retry after fixing the upstream cause instead of
    waiting for the exponential backoff TTL to expire.
    """
    if job_id not in RETRYABLE_INGEST_JOBS:
        raise HTTPException(404, f"ingest job is not retryable: {job_id}")

    await ingest_repo.clear_failures(job_id)
    # Prefix `queued:` so the frontend's `deriveIngestBadge` shows an
    # amber "queued" pill instead of red "error" while the background
    # task runs. The actual run will overwrite this row when it
    # finishes (success → `ok`, failure → real error message).
    await ingest_repo.record_health(
        job_id,
        ok=False,
        row_count=0,
        error="queued: manual retry — previous backoff cleared",
    )
    background_tasks.add_task(_run_ingest_job_once, job_id)
    return IngestRetryResult(
        status="queued",
        message=f"{RETRYABLE_INGEST_JOBS[job_id]} retry queued",
    )
