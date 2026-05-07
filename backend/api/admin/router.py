import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.system.router import VersionStatus
from auth.permissions import require_admin
from db.session import get_db
from models.alert import PriceAlert
from models.user import User, UserRole
from models.watchlist import Watchlist
from services import llm_key_service as keys
from services import llm_usage_service as usage
from services import market_key_service as market_keys
from services import persona_override_service as personas
from services import runtime_config_service as runtime_config
from services import system_task_config_service as system_tasks
from services.ingest import repository as ingest_repo
from services.deploy_service import read_deploy_status, trigger_deploy
from services.version_service import force_refresh_status, trigger_update

from .schemas import (
    ActiveUpdate,
    AdminUserItem,
    DeployStatusOut,
    DeployTriggerOut,
    IngestHealthOut,
    IngestRetryResult,
    LessonPromoteOut,
    LLMKeyInfo,
    LLMKeyUpsert,
    LLMKeyValidation,
    MarketKeyInfo,
    MarketKeyUpsert,
    MarketKeyValidation,
    PersonaConfigOut,
    PersonaOverrideIn,
    CategoricalSignalBucket,
    CategoricalSignalRow,
    NumericSignalRow,
    PostMortemGapRow,
    PostMortemGapsOut,
    SignalAuditHistoryOut,
    SignalAuditHistoryPoint,
    SignalAuditOut,
    SignalCoverageRow,
    SignalHallucinationRow,
    SignalQualityOut,
    RoleUpdate,
    SystemStats,
    RuntimeSettingIn,
    RuntimeSettingOut,
    SystemTaskConfigOut,
    SystemTaskOverrideIn,
    SystemTaskTestResult,
    UpdateResult,
    UsageBucketOut,
    UsageDayPoint,
    UsageSummaryOut,
)

router = APIRouter()
Admin = Annotated[dict, Depends(require_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]

# AdminPage proxy to the FinMind clone subsystem — separate sub-router
# under /api/admin/finmind so the React app uses its existing JWT
# admin auth instead of needing a separate X-Finmind-Admin-Key flow.
# See finmind_proxy.py for rationale on the architectural boundary.
from api.admin.finmind_proxy import router as _finmind_proxy_router  # noqa: E402

router.include_router(
    _finmind_proxy_router, prefix="/finmind", tags=["AdminFinMind"],
)

VALID_ROLES = {r.value for r in UserRole}
RETRYABLE_INGEST_JOBS = {
    "ingest_news_tw": "Taiwan news ingest",
    "ingest_news_international": "International news ingest",
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


@router.get("/stats", response_model=SystemStats)
async def stats(_: Admin, db: DB):
    total_users = await db.scalar(select(func.count(User.id)))
    active_users = await db.scalar(
        select(func.count(User.id)).where(User.is_active.is_(True))
    )
    by_role_rows = await db.execute(
        select(User.role, func.count(User.id)).group_by(User.role)
    )
    users_by_role = {row[0].value: row[1] for row in by_role_rows}

    total_alerts = await db.scalar(select(func.count(PriceAlert.id)))
    total_watchlists = await db.scalar(select(func.count(Watchlist.id)))

    return SystemStats(
        total_users=total_users or 0,
        active_users=active_users or 0,
        users_by_role=users_by_role,
        total_alerts=total_alerts or 0,
        total_watchlists=total_watchlists or 0,
    )


@router.get("/users", response_model=list[AdminUserItem])
async def list_users(
    _: Admin,
    db: DB,
    offset: int = 0,
    limit: int = 50,
):
    rows = await db.scalars(
        select(User).order_by(User.created_at.desc()).offset(offset).limit(limit)
    )
    return list(rows.all())


@router.patch("/users/{user_id}/role", status_code=204)
async def update_role(user_id: uuid.UUID, body: RoleUpdate, _: Admin, db: DB):
    if body.role not in VALID_ROLES:
        raise HTTPException(400, f"Invalid role. Must be one of: {VALID_ROLES}")
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user.role = UserRole(body.role)
    await db.commit()


@router.patch("/users/{user_id}/active", status_code=204)
async def update_active(user_id: uuid.UUID, body: ActiveUpdate, _: Admin, db: DB):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user.is_active = body.is_active
    await db.commit()


@router.post("/update", response_model=UpdateResult)
async def trigger_system_update(_: Admin) -> UpdateResult:
    result = await trigger_update()
    return UpdateResult(**result)


@router.post("/deploy", response_model=DeployTriggerOut)
async def trigger_full_deploy(user: Admin) -> DeployTriggerOut:
    """Touch the host trigger file; the host's finceptweb-deploy.path
    systemd unit picks up the mtime change and runs the deploy script
    asynchronously. Returns immediately with a `trigger_id` the
    frontend uses to track this specific run via /deploy/status.
    """
    result = await trigger_deploy(actor_id=user["id"])
    return DeployTriggerOut(**result)


@router.get("/deploy/status", response_model=DeployStatusOut)
async def get_deploy_status(_: Admin) -> DeployStatusOut:
    """Return the current deploy phase + metadata.

    Polled every 2s by the frontend RedeployCard. Tolerates the host
    file being absent (returns `{phase: "idle"}`) and unparseable
    (returns `{phase: "unknown"}`) so the polling query never errors.
    """
    raw = await read_deploy_status()
    return DeployStatusOut(**raw)


@router.post("/version/check", response_model=VersionStatus)
async def check_for_updates(_: Admin) -> VersionStatus:
    """Force a fresh GitHub lookup, bypassing the Redis cache.

    Same payload shape as `GET /api/system/version` so the frontend can drop
    the response straight into its `["version"]` query cache.
    """
    data = await force_refresh_status()
    return VersionStatus(**data)


# ── LLM provider keys ─────────────────────────────────────────────

def _info_to_schema(info: keys.KeyInfo) -> LLMKeyInfo:
    return LLMKeyInfo(
        provider=info.provider,
        has_key=info.has_key,
        source=info.source,
        masked=info.masked,
        last_validated_at=info.last_validated_at,
        last_validation_ok=info.last_validation_ok,
        last_validation_message=info.last_validation_message,
        updated_at=info.updated_at,
    )


@router.get("/llm-keys", response_model=list[LLMKeyInfo])
async def list_llm_keys(_: Admin, db: DB) -> list[LLMKeyInfo]:
    """List the current key state for every supported LLM provider.

    Each row reports whether a DB-stored key exists, an .env fallback is in
    use, or no key is set; the actual secret never leaves the server — only
    the masked tail is returned.
    """
    return [_info_to_schema(i) for i in await keys.list_keys(db)]


@router.put("/llm-keys/{provider}", response_model=LLMKeyInfo)
async def upsert_llm_key(
    provider: str, body: LLMKeyUpsert, user: Admin, db: DB,
) -> LLMKeyInfo:
    if provider not in keys.SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    try:
        info = await keys.upsert_key(db, provider, body.api_key, uuid.UUID(user["id"]))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _info_to_schema(info)


@router.delete("/llm-keys/{provider}", status_code=204)
async def delete_llm_key(provider: str, _: Admin, db: DB) -> None:
    if provider not in keys.SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    await keys.delete_key(db, provider)


# ── Per-persona model routing ────────────────────────────────────

def _persona_config_to_schema(p: personas.PersonaConfig) -> PersonaConfigOut:
    return PersonaConfigOut(
        persona_id=p.persona_id,
        name=p.name,
        description=p.description,
        default_provider=p.default_provider,
        default_model=p.default_model,
        effective_provider=p.effective_provider,
        effective_model=p.effective_model,
        is_overridden=p.is_overridden,
    )


@router.get("/personas", response_model=list[PersonaConfigOut])
async def list_personas(_: Admin, db: DB) -> list[PersonaConfigOut]:
    """List every persona with its compiled-default + currently-effective provider/model."""
    return [_persona_config_to_schema(p) for p in await personas.list_personas(db)]


@router.put("/personas/{persona_id}", response_model=PersonaConfigOut)
async def upsert_persona_override(
    persona_id: str, body: PersonaOverrideIn, user: Admin, db: DB,
) -> PersonaConfigOut:
    try:
        cfg = await personas.upsert_override(
            db, persona_id, body.provider, body.model, uuid.UUID(user["id"]),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _persona_config_to_schema(cfg)


@router.delete("/personas/{persona_id}", status_code=204)
async def delete_persona_override(persona_id: str, _: Admin, db: DB) -> None:
    await personas.delete_override(db, persona_id)


# ── Per-task LLM routing (background system tasks) ───────────────

def _system_task_to_schema(t: system_tasks.TaskConfig) -> SystemTaskConfigOut:
    return SystemTaskConfigOut(
        task_id=t.task_id,
        name=t.name,
        description=t.description,
        default_provider=t.default_provider,
        default_model=t.default_model,
        effective_provider=t.effective_provider,
        effective_model=t.effective_model,
        is_overridden=t.is_overridden,
        updated_at=t.updated_at,
        updated_by_email=t.updated_by_email,
    )


@router.get("/system-tasks", response_model=list[SystemTaskConfigOut])
async def list_system_tasks(_: Admin, db: DB) -> list[SystemTaskConfigOut]:
    """List every background task that supports admin LLM routing, with
    its compiled default and currently effective provider/model."""
    return [_system_task_to_schema(t) for t in await system_tasks.list_tasks(db)]


@router.put("/system-tasks/{task_id}", response_model=SystemTaskConfigOut)
async def upsert_system_task_override(
    task_id: str, body: SystemTaskOverrideIn, user: Admin, db: DB,
) -> SystemTaskConfigOut:
    try:
        cfg = await system_tasks.upsert_override(
            db, task_id, body.provider, body.model, uuid.UUID(user["id"]),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _system_task_to_schema(cfg)


@router.delete("/system-tasks/{task_id}", status_code=204)
async def delete_system_task_override(task_id: str, _: Admin, db: DB) -> None:
    try:
        await system_tasks.delete_override(db, task_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post(
    "/system-tasks/{task_id}/test", response_model=SystemTaskTestResult,
)
async def test_system_task(
    task_id: str, _: Admin, db: DB,
) -> SystemTaskTestResult:
    """Smoke-test the task's resolved provider/model with a 1-token ping.

    Uses whatever override is currently saved (or the compiled default if
    none). Surface this in the admin UI so operators can verify a fresh
    API key + model combo works before relying on the next scheduled run.
    """
    try:
        result = await system_tasks.test_task(db, task_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return SystemTaskTestResult(**result.__dict__)


# ── Runtime tunables (admin-tunable env-var overrides) ───────────

def _runtime_setting_to_schema(s: runtime_config.SettingInfo) -> RuntimeSettingOut:
    return RuntimeSettingOut(
        key=s.key,
        type=s.type,
        name=s.name,
        description=s.description,
        min_value=s.min_value,
        max_value=s.max_value,
        default_value=s.default_value,
        effective_value=s.effective_value,
        is_overridden=s.is_overridden,
        updated_at=s.updated_at,
        updated_by_email=s.updated_by_email,
    )


@router.get("/runtime-settings", response_model=list[RuntimeSettingOut])
async def list_runtime_settings(_: Admin, db: DB) -> list[RuntimeSettingOut]:
    """List every admin-tunable env-var override with its compiled
    default + currently effective value + audit info."""
    return [_runtime_setting_to_schema(s) for s in await runtime_config.list_settings(db)]


@router.put("/runtime-settings/{key}", response_model=RuntimeSettingOut)
async def upsert_runtime_setting(
    key: str, body: RuntimeSettingIn, user: Admin, db: DB,
) -> RuntimeSettingOut:
    try:
        cfg = await runtime_config.upsert(
            db, key, body.value, updated_by_id=uuid.UUID(user["id"]),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _runtime_setting_to_schema(cfg)


@router.delete("/runtime-settings/{key}", status_code=204)
async def delete_runtime_setting(key: str, _: Admin, db: DB) -> None:
    try:
        await runtime_config.delete_override(db, key)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


# ── LLM usage summary (admin-wide) ───────────────────────────────

def _summary_to_schema(s: usage.UsageSummary) -> UsageSummaryOut:
    return UsageSummaryOut(
        range_days=s.range_days,
        user_scoped=s.user_scoped,
        total_requests=s.total_requests,
        total_prompt_tokens=s.total_prompt_tokens,
        total_completion_tokens=s.total_completion_tokens,
        total_cost_usd=s.total_cost_usd,
        by_provider=[UsageBucketOut(**b.__dict__) for b in s.by_provider],
        by_day=[UsageDayPoint(**d) for d in s.by_day],
    )


@router.get("/llm-usage", response_model=UsageSummaryOut)
async def admin_llm_usage(
    _: Admin, db: DB, range_days: int = 30,
) -> UsageSummaryOut:
    """System-wide LLM usage aggregate for the last `range_days` days."""
    range_days = max(1, min(range_days, 365))
    summary = await usage.usage_summary(db, range_days=range_days)
    return _summary_to_schema(summary)


# ── Scheduled ingest health ──────────────────────────────────────

@router.get("/ingest/health", response_model=list[IngestHealthOut])
async def ingest_health(_: Admin) -> list[IngestHealthOut]:
    """Per-job snapshot for every scheduled ingest task.

    State lives in Redis with a 7-day TTL; entries disappear after a
    week of silence so a removed job doesn't linger forever.
    """
    return [
        IngestHealthOut(
            job_id=h.job_id,
            last_run_at=h.last_run_at,
            ok=h.ok,
            row_count=h.row_count,
            error=h.error,
        )
        for h in await ingest_repo.list_health()
    ]


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
    job_id: str, background_tasks: BackgroundTasks, _: Admin,
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


@router.post("/llm-keys/{provider}/test", response_model=LLMKeyValidation)
async def test_llm_key(provider: str, _: Admin, db: DB) -> LLMKeyValidation:
    """Make a tiny live call to the provider to verify the saved key works.

    Resolves the active key (DB → .env fallback) and returns ok/false plus
    the provider's error message if any. Persists the result onto the row
    so the UI can show a 'last validated' badge.
    """
    if provider not in keys.SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    key = await keys.resolve_key(db, provider)
    if not key:
        return LLMKeyValidation(ok=False, message="no key configured")
    result = await keys.validate_key(provider, key)
    await keys.record_validation(db, provider, result)
    return LLMKeyValidation(ok=result.ok, message=result.message)


# ── Market-data provider keys ─────────────────────────────────────

def _market_info_to_schema(info: market_keys.KeyInfo) -> MarketKeyInfo:
    return MarketKeyInfo(
        provider=info.provider,
        has_key=info.has_key,
        source=info.source,
        masked=info.masked,
        last_validated_at=info.last_validated_at,
        last_validation_ok=info.last_validation_ok,
        last_validation_message=info.last_validation_message,
        updated_at=info.updated_at,
    )


@router.get("/market-keys", response_model=list[MarketKeyInfo])
async def list_market_keys(_: Admin, db: DB) -> list[MarketKeyInfo]:
    """List the current key state for every supported market-data provider.

    Mirrors /llm-keys: each row reports DB / env / none, and only the
    masked tail of the secret is ever returned.
    """
    return [_market_info_to_schema(i) for i in await market_keys.list_keys(db)]


@router.put("/market-keys/{provider}", response_model=MarketKeyInfo)
async def upsert_market_key(
    provider: str, body: MarketKeyUpsert, user: Admin, db: DB,
) -> MarketKeyInfo:
    if provider not in market_keys.SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    try:
        info = await market_keys.upsert_key(
            db, provider, body.api_key, uuid.UUID(user["id"]),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _market_info_to_schema(info)


@router.delete("/market-keys/{provider}", status_code=204)
async def delete_market_key(provider: str, _: Admin, db: DB) -> None:
    if provider not in market_keys.SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    await market_keys.delete_key(db, provider)


@router.post("/market-keys/{provider}/test", response_model=MarketKeyValidation)
async def test_market_key(provider: str, _: Admin, db: DB) -> MarketKeyValidation:
    """Resolve the active key (DB → env) and ping the provider.

    For Finnhub this hits /quote?symbol=AAPL — the same endpoint the
    connector uses, so a 200 with a non-zero current price proves both
    that the key was honoured and the network path works."""
    if provider not in market_keys.SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    key = await market_keys.resolve_key(provider)
    if not key:
        return MarketKeyValidation(ok=False, message="no key configured")
    result = await market_keys.validate_key(provider, key)
    await market_keys.record_validation(db, provider, result)
    return MarketKeyValidation(ok=result.ok, message=result.message)


# ── Signal-citation audit (PR #239-#240 surface) ─────────────────


_SIGNAL_AUDIT_RECENT_MAX = 200


@router.get("/signal-audit", response_model=SignalAuditOut)
async def signal_audit(
    _: Admin, db: DB,
    recent: int = 30,
    market: str | None = None,
) -> SignalAuditOut:
    """Run the same bulk audit as `python -m scripts.audit_signal_usage
    --recent N` and return the result as JSON for the AdminPage UI.

    `recent` is bounded at `_SIGNAL_AUDIT_RECENT_MAX` so a typo'd
    request can't fan out across the entire archive (each audited
    discussion = 2 SQL reads + per-turn regex scan; bulk over thousands
    of rows would take seconds + saturate the connection pool).

    `market` filters concluded discussions by market when set; default
    is all-markets.

    Coverage rows are pre-sorted ascending by `citation_rate` so the
    UI table renders zero-uptake offenders at the top without any
    client-side sort logic.
    """
    from services.signal_audit_service import audit_recent_discussions

    if recent < 1 or recent > _SIGNAL_AUDIT_RECENT_MAX:
        raise HTTPException(
            400,
            f"recent must be in [1, {_SIGNAL_AUDIT_RECENT_MAX}]; got {recent}",
        )

    summary = await audit_recent_discussions(
        db, limit=recent, market=market,
    )

    coverage_rows = []
    for sig, stats in summary.coverage.items():
        denom = stats["persona_count"]
        rate = (stats["cited"] / denom) if denom else 0.0
        cited_with_value = stats.get("cited_with_value", 0)
        value_rate = (cited_with_value / denom) if denom else 0.0
        coverage_rows.append(SignalCoverageRow(
            signal=sig,
            present=stats["present"],
            cited=stats["cited"],
            cited_with_value=cited_with_value,
            persona_count=denom,
            citation_rate=round(rate, 4),
            value_citation_rate=round(value_rate, 4),
        ))
    coverage_rows.sort(key=lambda r: (r.citation_rate, r.signal))

    hallucinations = []
    for sig, stats in summary.hallucinations.items():
        if stats["hallucinated"] <= 0:
            continue
        denom = stats["persona_count_absent"]
        rate = (stats["hallucinated"] / denom) if denom else 0.0
        hallucinations.append(SignalHallucinationRow(
            signal=sig,
            absent_rounds=stats["absent_rounds"],
            hallucinated=stats["hallucinated"],
            persona_count_absent=denom,
            hallucination_rate=round(rate, 4),
        ))
    hallucinations.sort(
        key=lambda r: (-r.hallucination_rate, r.signal),
    )

    zero_uptake = sorted(
        sig for sig, stats in summary.coverage.items()
        if stats["cited"] == 0 and stats["persona_count"] > 0
    )

    # PR #263: include per-signal trend history for the sparkline
    # column. Bulk read in one query so the frontend doesn't fan out
    # to N per-signal calls. Failures here are non-fatal — sparkline
    # is decorative.
    history: dict[str, list[SignalAuditHistoryPoint]] = {}
    try:
        from services.signal_audit_service import read_all_signals_history

        bulk = await read_all_signals_history(
            db, market=market, days=30,
        )
        history = {
            sig: [SignalAuditHistoryPoint(**p) for p in points]
            for sig, points in bulk.items()
        }
    except Exception:
        # Decorative — main response still useful without it.
        history = {}

    return SignalAuditOut(
        discussions_audited=summary.discussions_audited,
        discussion_ids=summary.discussion_ids,
        coverage=coverage_rows,
        zero_uptake=zero_uptake,
        hallucinations=hallucinations,
        history=history,
    )


# ── Signal-audit history sparkline (PR #263) ─────────────────────


_SIGNAL_AUDIT_HISTORY_DAYS_MAX = 365


@router.get(
    "/signal-audit-history",
    response_model=SignalAuditHistoryOut,
)
async def signal_audit_history(
    _: Admin, db: DB,
    signal: str,
    market: str | None = None,
    days: int = 30,
) -> SignalAuditHistoryOut:
    """Per-signal daily snapshot timeseries persisted by the
    `snapshot_signal_audit` cron. Supports the AdminPage sparkline
    column on the SignalAuditCard so admins can see whether a
    signal's citation rate is improving or deteriorating across
    deploys.

    `days` is bounded — pulling years of per-signal history at
    O(N rows × M signals) costs nothing on Postgres but slows the
    frontend; the bound keeps the JSON payload small.
    """
    from services.signal_audit_service import read_signal_history

    if days < 1 or days > _SIGNAL_AUDIT_HISTORY_DAYS_MAX:
        raise HTTPException(
            400,
            f"days must be in [1, {_SIGNAL_AUDIT_HISTORY_DAYS_MAX}]; "
            f"got {days}",
        )
    if not signal:
        raise HTTPException(400, "signal query param is required")

    points_data = await read_signal_history(
        db, signal=signal, market=market, days=days,
    )
    return SignalAuditHistoryOut(
        signal=signal,
        market=market,
        points=[SignalAuditHistoryPoint(**p) for p in points_data],
    )


# ── Post-mortem data-gap analysis (PR #262) ──────────────────────


_POST_MORTEM_GAPS_RECENT_MAX = 200


@router.get("/post-mortem-gaps", response_model=PostMortemGapsOut)
async def post_mortem_gaps(
    _: Admin, db: DB,
    recent: int = 30,
    market: str | None = None,
) -> PostMortemGapsOut:
    """Aggregate "missing data" mentions extracted from post-mortem
    persona reflections (PR #249 self-critique flow). Each persona
    answer to "what data is missing?" is keyword-matched against a
    curated taxonomy of data categories the platform doesn't yet
    provide; the roll-up surfaces the most-requested gaps as a
    prioritized backlog.

    `recent` is bounded — same blast-radius reasoning as the signal
    audit endpoint. `market` filters concluded discussions when set.

    Empty `gaps` list means either no concluded discussions in the
    window had post-mortem rounds, OR they did but personas didn't
    mention any taxonomy category. Both states are honest signals
    for the operator.
    """
    from services.post_mortem_analysis_service import (
        analyze_recent_post_mortems,
    )

    if recent < 1 or recent > _POST_MORTEM_GAPS_RECENT_MAX:
        raise HTTPException(
            400,
            f"recent must be in [1, {_POST_MORTEM_GAPS_RECENT_MAX}]; "
            f"got {recent}",
        )

    summary = await analyze_recent_post_mortems(
        db, limit=recent, market=market,
    )
    return PostMortemGapsOut(
        discussions_audited=summary.discussions_audited,
        discussion_ids=summary.discussion_ids,
        gaps=[
            PostMortemGapRow(
                category=g.category,
                mentions=g.mentions,
                discussions_mentioning=g.discussions_mentioning,
                persona_count_mentioning=g.persona_count_mentioning,
            )
            for g in summary.gaps
        ],
    )


# ── Empirical signal quality (PR #250 service surface) ───────────


_SIGNAL_QUALITY_LOOKBACK_MAX = 365


@router.get("/signal-quality", response_model=SignalQualityOut)
async def signal_quality(
    _: Admin, db: DB,
    lookback: int = 60,
    market: str | None = None,
) -> SignalQualityOut:
    """Return the empirical signal-vs-D5-return correlation report
    (PR #250 service surface) so the AdminPage can render which
    signals actually predict moves vs which are noise.

    Numeric signals (RSI / volume_ratio / industry_rs / …) get
    Pearson r + sign-match accuracy. Categorical signals
    (taifex.trend / day_trading_trend / …) get mean D5 per bucket.

    `lookback` capped at ~1 year — beyond that the dataset starts
    mixing across very different market regimes and the aggregate
    becomes harder to interpret. Operator who needs longer windows
    should run the CLI directly (`python -m scripts.signal_quality_check`).

    Read-only path; same admin gate as the audit endpoint.
    """
    from services.signal_quality_service import compute_signal_quality

    if lookback < 1 or lookback > _SIGNAL_QUALITY_LOOKBACK_MAX:
        raise HTTPException(
            400,
            f"lookback must be in [1, {_SIGNAL_QUALITY_LOOKBACK_MAX}]; "
            f"got {lookback}",
        )

    report = await compute_signal_quality(
        db, lookback_days=lookback, market=market,
    )

    numeric_rows = [
        NumericSignalRow(
            label=row.label,
            path=row.path,
            n=row.n,
            mean_value=row.mean_value,
            mean_d5_return=row.mean_d5_return,
            pearson_r=row.pearson_r,
            sign_match_pct=row.sign_match_pct,
        )
        for row in report.numeric
    ]
    categorical_rows = [
        CategoricalSignalRow(
            label=row.label,
            path=row.path,
            n_total=row.n_total,
            buckets=[
                CategoricalSignalBucket(
                    category=cat,
                    n=bucket.n,
                    mean_d5_return=bucket.mean_d5_return,
                    median_d5_return=bucket.median_d5_return,
                )
                for cat, bucket in row.by_category.items()
            ],
        )
        for row in report.categorical
    ]

    return SignalQualityOut(
        discussions_audited=report.discussions_audited,
        discussion_ids=report.discussion_ids,
        lookback_days=report.lookback_days,
        market=report.market,
        numeric=numeric_rows,
        categorical=categorical_rows,
    )


@router.post(
    "/lessons/{lesson_id}/promote",
    response_model=LessonPromoteOut,
)
async def promote_lesson_to_structural(
    lesson_id: int,
    admin: Admin,
    db: DB,
) -> LessonPromoteOut:
    """PR-B2 follow-up — admin manual promotion of a lesson into the
    `structural` tier so it stops decaying entirely.

    The automatic episodic→semantic promotion lives in
    `lesson_tier_service.promote_eligible_lessons` (driven by
    Phase 3 of the sweep worker via usage + hit-rate gates).
    structural is intentionally admin-only because the threshold
    of "this is permanent advice" is qualitative judgement the
    system can't reliably infer. Once promoted, the only way out
    is another admin call — `services.lesson_tier_service.
    promote_to_structural` is idempotent so re-flipping a row
    doesn't error.

    Owner-scoped: each admin operates on their OWN learning
    history, mirroring the rest of the lesson API. Cross-owner
    promote returns 404 (don't reveal foreign-owner row
    existence). Multi-admin deployments where one admin needs to
    promote another's lessons should use a future explicitly-
    cross-tenant endpoint (none today by design).

    Returns the post-promotion state so the AdminPage UI can
    confirm the tier flip stuck without a follow-up GET.
    """
    from services.lesson_tier_service import promote_to_structural
    row = await promote_to_structural(
        db, lesson_id=lesson_id, owner_id=uuid.UUID(admin["id"]),
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"lesson {lesson_id} not found",
        )
    return LessonPromoteOut(
        id=row.id,
        market=row.market,
        category=row.category,
        tier=row.tier,
        usage_count=row.usage_count,
        hit_count=row.hit_count,
        promoted_at=row.promoted_at,
        lesson_text=row.lesson_text,
    )
