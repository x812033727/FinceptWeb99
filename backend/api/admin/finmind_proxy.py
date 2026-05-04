"""Main-app proxy to the FinMind clone subsystem.

Crosses the architectural boundary documented in CLAUDE.md (the
FinMind clone is normally consumed via HTTP, not in-process imports)
ONLY for the AdminPage. Rationale:

  - AdminPage already has main-app JWT admin auth — wiring a separate
    `X-Finmind-Admin-Key` flow into the React app for one card would
    be nonsensical UX.
  - Operator workflow (toggle enabled, watch usage) is purely local
    — there's no scaling reason to round-trip through HTTP.

Customer-facing traffic still goes through `/api/finmind/...` with
its own auth + quota; this proxy is a separate concern.

Endpoints (all gated by main-app admin role):
  GET   /api/admin/finmind/datasets
  PATCH /api/admin/finmind/datasets/{dataset_code}
  GET   /api/admin/finmind/usage?days=N
  GET   /api/admin/finmind/status

The PATCH body schema mirrors `finmind.api.schemas.DatasetSourceUpdate`
so the frontend doesn't have to know it's a proxy.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin
from finmind.db.session import get_finmind_db
from finmind.models.dataset_source import DatasetSource
from models.user import User

log = logging.getLogger("api.admin.finmind_proxy")

router = APIRouter()


Admin = Annotated[User, Depends(require_admin)]
FmDb = Annotated[AsyncSession, Depends(get_finmind_db)]

_VALID_SOURCES = {"finmind", "twse", "tpex", "taifex", "mops", "tdcc"}


class FinmindDatasetItem(BaseModel):
    """One row in `GET /finmind/datasets`. Schema duplicated from
    `finmind.api.schemas.DatasetSourceItem` (rather than re-exported)
    so the AdminPage frontend has a stable contract that's independent
    of the FinMind subsystem's internal Pydantic versioning."""

    dataset_code: str
    category: str
    description_zh: str
    local_table: str
    per_symbol: bool
    primary_source: str
    fallback_source: str | None
    active_source: str
    enabled: bool
    sponsor_tier: bool
    ingest_freq: str
    last_ingest_at: str | None
    last_ingest_rows: int | None
    last_error: str | None


class FinmindDatasetUpdate(BaseModel):
    enabled: bool | None = None
    active_source: str | None = None


@router.get(
    "/datasets",
    response_model=list[FinmindDatasetItem],
    summary="AdminPage: list every FinMind dataset",
)
async def list_finmind_datasets(_: Admin, db: FmDb) -> list[FinmindDatasetItem]:
    rows = (
        await db.execute(
            select(DatasetSource).order_by(DatasetSource.dataset_code)
        )
    ).scalars().all()
    return [
        FinmindDatasetItem(
            dataset_code=r.dataset_code,
            category=r.category,
            description_zh=r.description_zh,
            local_table=r.local_table,
            per_symbol=r.per_symbol,
            primary_source=r.primary_source,
            fallback_source=r.fallback_source,
            active_source=r.active_source,
            enabled=r.enabled,
            sponsor_tier=r.sponsor_tier,
            ingest_freq=r.ingest_freq,
            last_ingest_at=(
                r.last_ingest_at.isoformat() if r.last_ingest_at else None
            ),
            last_ingest_rows=r.last_ingest_rows,
            last_error=r.last_error,
        )
        for r in rows
    ]


@router.patch(
    "/datasets/{dataset_code}",
    response_model=FinmindDatasetItem,
    summary="AdminPage: toggle enabled / flip active_source per dataset",
)
async def update_finmind_dataset(
    dataset_code: str,
    body: FinmindDatasetUpdate,
    _: Admin,
    db: FmDb,
) -> FinmindDatasetItem:
    row = await db.get(DatasetSource, dataset_code)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown dataset: {dataset_code}",
        )
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.active_source is not None:
        if body.active_source not in _VALID_SOURCES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"active_source must be one of "
                    f"{sorted(_VALID_SOURCES)}; got '{body.active_source}'"
                ),
            )
        row.active_source = body.active_source
    await db.commit()
    await db.refresh(row)
    return FinmindDatasetItem(
        dataset_code=row.dataset_code,
        category=row.category,
        description_zh=row.description_zh,
        local_table=row.local_table,
        per_symbol=row.per_symbol,
        primary_source=row.primary_source,
        fallback_source=row.fallback_source,
        active_source=row.active_source,
        enabled=row.enabled,
        sponsor_tier=row.sponsor_tier,
        ingest_freq=row.ingest_freq,
        last_ingest_at=(
            row.last_ingest_at.isoformat() if row.last_ingest_at else None
        ),
        last_ingest_rows=row.last_ingest_rows,
        last_error=row.last_error,
    )


@router.get(
    "/status",
    summary="AdminPage: catalog + Phase 1 coverage + backfill summary",
)
async def finmind_status(_: Admin, db: FmDb) -> dict[str, Any]:
    """Calls into the existing `finmind.scripts.status.collect_status`
    so the AdminPage card and the CLI report show the same numbers.
    Same dataclass, JSON-serialized."""
    from dataclasses import asdict

    from finmind.scripts.status import collect_status

    report = await collect_status(db)
    payload = asdict(report)
    # Coerce datetime → str for JSON; collect_status' `generated_at`
    # is already a string, but the recent_errors list contains
    # serialized timestamps that are already strings too.
    return payload


# ── Plan management ─────────────────────────────────────────────


class PlanItem(BaseModel):
    code: str
    name: str
    price_monthly: float | None
    price_yearly: float | None
    currency: str
    allowed_datasets: list[str] | None
    quota_daily_calls: int
    quota_daily_rows: int
    enabled: bool


class PlanUpsert(BaseModel):
    """Both POST (create) and PUT (update) take this shape — `code` is
    the primary key, idempotent UPSERT semantics."""

    name: str
    price_monthly: float | None = None
    price_yearly: float | None = None
    currency: str = "TWD"
    allowed_datasets: list[str] | None = None
    quota_daily_calls: int = 1000
    quota_daily_rows: int = 100_000
    enabled: bool = True


def _plan_to_item(p) -> PlanItem:
    return PlanItem(
        code=p.code,
        name=p.name,
        price_monthly=float(p.price_monthly) if p.price_monthly else None,
        price_yearly=float(p.price_yearly) if p.price_yearly else None,
        currency=p.currency,
        allowed_datasets=p.allowed_datasets,
        quota_daily_calls=p.quota_daily_calls,
        quota_daily_rows=p.quota_daily_rows,
        enabled=p.enabled,
    )


@router.get(
    "/plans",
    response_model=list[PlanItem],
    summary="AdminPage: list every pricing plan",
)
async def list_plans(_: Admin, db: FmDb) -> list[PlanItem]:
    from finmind.models.billing import Plan

    rows = (
        await db.execute(select(Plan).order_by(Plan.code))
    ).scalars().all()
    return [_plan_to_item(p) for p in rows]


@router.put(
    "/plans/{code}",
    response_model=PlanItem,
    summary="AdminPage: create-or-update a plan (UPSERT on code)",
)
async def upsert_plan(
    code: str, body: PlanUpsert, _: Admin, db: FmDb,
) -> PlanItem:
    """Idempotent UPSERT — same endpoint creates a new plan AND updates
    an existing one. Path `code` is the source of truth."""
    from finmind.models.billing import Plan

    existing = await db.get(Plan, code)
    if existing is None:
        existing = Plan(
            code=code,
            name=body.name,
            price_monthly=body.price_monthly,
            price_yearly=body.price_yearly,
            currency=body.currency,
            allowed_datasets=body.allowed_datasets,
            quota_daily_calls=body.quota_daily_calls,
            quota_daily_rows=body.quota_daily_rows,
            enabled=body.enabled,
        )
        db.add(existing)
    else:
        existing.name = body.name
        existing.price_monthly = body.price_monthly
        existing.price_yearly = body.price_yearly
        existing.currency = body.currency
        existing.allowed_datasets = body.allowed_datasets
        existing.quota_daily_calls = body.quota_daily_calls
        existing.quota_daily_rows = body.quota_daily_rows
        existing.enabled = body.enabled
    await db.commit()
    await db.refresh(existing)
    return _plan_to_item(existing)


@router.delete(
    "/plans/{code}",
    status_code=204,
    summary="AdminPage: disable a plan (soft — keeps subscriptions valid)",
)
async def disable_plan(code: str, _: Admin, db: FmDb) -> None:
    """Soft disable — flips `enabled=false`. Existing subscriptions
    on this plan keep working (the resolve-plan-limits path falls
    back to free-tier defaults when the plan is disabled, so cust-
    omers gracefully degrade rather than getting 503'd)."""
    from sqlalchemy import update

    from finmind.models.billing import Plan

    result = await db.execute(
        update(Plan).where(Plan.code == code).values(enabled=False)
    )
    if result.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown plan: {code}",
        )
    await db.commit()


# ── API key management ─────────────────────────────────────────


class IssueKeyRequest(BaseModel):
    owner_email: str
    name: str | None = None
    # Optional: link to a plan via auto-created Subscription. When
    # None / absent / unknown, key starts on free-tier defaults
    # (100 calls / 10K rows per day per
    # `finmind.billing.quota._FREE_TIER_*`).
    plan_code: str | None = None


class IssuedKeyResponse(BaseModel):
    """Plaintext is exposed ONCE at issuance and never readable again
    (we only store the sha256). Frontend must copy + persist out-of-
    band immediately — there is no "show me again later" path."""

    record_id: int
    plaintext: str
    prefix: str
    owner_email: str
    plan_code: str | None
    subscription_id: int | None


class ApiKeyItem(BaseModel):
    """Listing shape — never includes plaintext or hash."""

    id: int
    prefix: str
    owner_email: str
    name: str | None
    enabled: bool
    expires_at: str | None
    last_used_at: str | None
    created_at: str
    plan_code: str | None  # joined from subscriptions
    subscription_id: int | None


@router.post(
    "/keys",
    response_model=IssuedKeyResponse,
    summary="AdminPage: issue a new fck_live_ key (with optional plan)",
)
async def issue_finmind_key(
    body: IssueKeyRequest, _: Admin, db: FmDb,
) -> IssuedKeyResponse:
    """Generates a fresh `fck_live_<prefix><suffix>` key, persists
    sha256 + prefix only, and returns the plaintext for one-time
    display.

    When `plan_code` is supplied AND a matching enabled Plan exists,
    creates a Subscription (status='active', started_at=today) and
    links the new ApiKey to it. The customer's quota then comes
    from `plans.quota_daily_*` instead of free-tier defaults.

    Unknown / disabled plan_code falls back silently to free-tier
    (rather than 4xx-erroring) — operator can spot the missing link
    in the keys table and re-link explicitly."""
    from datetime import date

    from finmind.billing.keys import issue_key
    from finmind.models.billing import Plan, Subscription

    subscription_id: int | None = None
    resolved_plan_code: str | None = None
    if body.plan_code:
        plan = await db.get(Plan, body.plan_code)
        if plan is not None and plan.enabled:
            sub = Subscription(
                owner_email=body.owner_email,
                plan_code=body.plan_code,
                status="active",
                started_at=date.today(),
                expires_at=None,
                external_provider=None,  # operator-issued, no Stripe link
                external_sub_id=None,
                auto_renew=False,
            )
            db.add(sub)
            await db.commit()
            await db.refresh(sub)
            subscription_id = sub.id
            resolved_plan_code = body.plan_code

    issued = await issue_key(
        db,
        owner_email=body.owner_email,
        name=body.name,
        subscription_id=subscription_id,
    )
    return IssuedKeyResponse(
        record_id=issued.record_id,
        plaintext=issued.plaintext,
        prefix=issued.prefix,
        owner_email=body.owner_email,
        plan_code=resolved_plan_code,
        subscription_id=subscription_id,
    )


@router.get(
    "/keys",
    response_model=list[ApiKeyItem],
    summary="AdminPage: list every issued key (no plaintext / hash)",
)
async def list_finmind_keys(_: Admin, db: FmDb) -> list[ApiKeyItem]:
    """Joins api_keys → subscriptions to surface plan_code per row.
    Free-tier keys (no subscription) show plan_code=None — frontend
    renders these with a muted "free" badge."""
    from finmind.models.billing import ApiKey, Subscription

    # LEFT JOIN — include keys with no subscription (free-tier).
    rows = (
        await db.execute(
            select(ApiKey, Subscription.plan_code)
            .outerjoin(
                Subscription, ApiKey.subscription_id == Subscription.id,
            )
            .order_by(ApiKey.created_at.desc())
        )
    ).all()
    return [
        ApiKeyItem(
            id=r[0].id,
            prefix=r[0].prefix,
            owner_email=r[0].owner_email,
            name=r[0].name,
            enabled=r[0].enabled,
            expires_at=r[0].expires_at.isoformat() if r[0].expires_at else None,
            last_used_at=(
                r[0].last_used_at.isoformat() if r[0].last_used_at else None
            ),
            created_at=r[0].created_at.isoformat(),
            plan_code=r[1],
            subscription_id=r[0].subscription_id,
        )
        for r in rows
    ]


@router.delete(
    "/keys/{key_id}",
    status_code=204,
    summary="AdminPage: disable a key (soft-revoke; keeps audit trail)",
)
async def revoke_finmind_key(key_id: int, _: Admin, db: FmDb) -> None:
    """Soft-revoke — sets enabled=false rather than DELETE so:
      - api_usage_events FK references stay valid
      - audit history (when, who issued / who revoked) survives
      - key can be re-enabled if revocation was a mistake

    Hard-delete would need a separate DELETE-with-FK-cascade endpoint
    that we don't expose intentionally."""
    from sqlalchemy import update

    from finmind.models.billing import ApiKey

    result = await db.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id)
        .values(enabled=False)
    )
    if result.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown key id: {key_id}",
        )
    await db.commit()


class RunDatasetRequest(BaseModel):
    """Optional body for `POST /datasets/{code}/run`. All fields
    optional — defaults match the cron's behavior for that dataset."""

    symbol: str | None = None
    start_date: str | None = None  # YYYY-MM-DD
    end_date: str | None = None


class RunDatasetResult(BaseModel):
    """One ChunkResult flattened for the frontend."""

    dataset_code: str
    symbol: str | None
    range_start: str
    range_end: str
    status: str           # 'done' | 'failed' | 'skipped'
    rows_written: int
    error: str | None


@router.post(
    "/datasets/{dataset_code}/run",
    response_model=RunDatasetResult,
    summary="AdminPage: trigger one ingest_chunk synchronously",
)
async def run_dataset(
    dataset_code: str,
    body: RunDatasetRequest,
    _: Admin,
    db: FmDb,
) -> RunDatasetResult:
    """Manual one-shot ingest for a single dataset. Synchronous —
    waits for the ingest to complete and returns the outcome so the
    AdminPage can show success/failure inline.

    For per-symbol datasets, supplying `symbol` runs that one symbol;
    omitting it falls through to symbol=None (most upstream sources
    fail without a symbol — runner records 'failed' in
    backfill_progress with a clear error).

    `start_date` / `end_date` default to the last 7 days (matches the
    cron's default backfill window).
    """
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    from finmind.ingest.runner import ingest_chunk
    from finmind.models.dataset_source import DatasetSource as DS

    ds = await db.get(DS, dataset_code)
    if ds is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown dataset: {dataset_code}",
        )
    if not ds.local_table:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"{dataset_code}: destination table not yet built — "
                "Phase 1 hasn't migrated this dataset"
            ),
        )

    end = (
        _dt.strptime(body.end_date, "%Y-%m-%d").date()
        if body.end_date
        else _dt.now(tz=_tz.utc).date()
    )
    start = (
        _dt.strptime(body.start_date, "%Y-%m-%d").date()
        if body.start_date
        else end - _td(days=7)
    )

    # Wrap in try/except so a runtime error inside ingest_chunk OR
    # any of its transitive dependencies (FinMind connector, db
    # transaction, mapping resolution) surfaces as a 500 with a
    # human-readable detail field instead of FastAPI's default
    # generic 500-page. Frontend reads `error.response.data.detail`
    # to render the actual cause.
    try:
        result = await ingest_chunk(
            db,
            dataset_code=dataset_code,
            symbol=body.symbol,
            range_start=start,
            range_end=end,
        )
    except Exception as exc:
        log.exception("run_dataset crashed: %s", dataset_code)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"ingest_chunk raised: {exc.__class__.__name__}: {exc!s}",
        ) from exc

    return RunDatasetResult(
        dataset_code=dataset_code,
        symbol=body.symbol,
        range_start=start.isoformat(),
        range_end=end.isoformat(),
        status=result.status,
        rows_written=result.rows_written,
        error=result.error,
    )


class RunDueResponse(BaseModel):
    """`POST /run-due` summary — frontend renders the totals + a
    per-chunk breakdown."""

    total: int
    done: int
    failed: int
    skipped: int
    rows_written: int
    outcomes: list[RunDatasetResult]


@router.post(
    "/run-due",
    response_model=RunDueResponse,
    summary=(
        "AdminPage: trigger run_due_now (every enabled dataset whose "
        "last_ingest_at is stale, fanned across tw_stock_info universe)"
    ),
)
async def run_due(_: Admin, db: FmDb) -> RunDueResponse:
    """Manual "refresh now" button — same logic as the cron auto-runner.

    Walks every enabled dataset whose last_ingest_at is stale relative
    to its ingest_freq, and for per-symbol datasets fans across the
    universe currently in `tw_stock_info`. Synchronous — the operator
    waits for the full sweep to complete (typically a few seconds
    when most datasets are fresh).

    NOT designed for the initial-deploy "ingest every dataset for
    every symbol for 10 years of history" workflow — that's the
    backfill CLI's job. This is the daily refresh button."""
    from finmind.scheduler.runner import (
        get_universe_from_tw_stock_info,
        run_due_now,
    )

    # Same defensive try/except pattern as run_dataset above —
    # surface the real cause (e.g. "OperationalError: relation
    # 'tw_stock_info' does not exist" → operator forgot init_db,
    # or "ConnectionRefusedError" → finmind_clone DB not reachable)
    # to the frontend instead of a generic 500.
    try:
        universe = await get_universe_from_tw_stock_info(db)
        outcomes = await run_due_now(db, symbols=universe)
    except Exception as exc:
        log.exception("run_due crashed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"run_due_now raised: {exc.__class__.__name__}: "
                f"{exc!s}. Most likely causes: (1) `python -m "
                f"finmind.scripts.init_db` not run yet — required "
                f"after first deploy or after migrations 0001-0011; "
                f"(2) finmind_clone DB unreachable at "
                f"FINMIND_DATABASE_URL."
            ),
        ) from exc

    items = [
        RunDatasetResult(
            dataset_code=o.chunk.dataset_code,
            symbol=o.chunk.symbol,
            range_start=o.chunk.range_start.isoformat(),
            range_end=o.chunk.range_end.isoformat(),
            status=o.result.status,
            rows_written=o.result.rows_written,
            error=o.result.error,
        )
        for o in outcomes
    ]
    return RunDueResponse(
        total=len(items),
        done=sum(1 for i in items if i.status == "done"),
        failed=sum(1 for i in items if i.status == "failed"),
        skipped=sum(1 for i in items if i.status == "skipped"),
        rows_written=sum(i.rows_written for i in items),
        outcomes=items,
    )


@router.get(
    "/usage",
    summary="AdminPage: per-day / per-dataset / per-key usage rollup",
)
async def finmind_usage(
    _: Admin,
    db: FmDb,
    days: int = 7,
) -> dict[str, Any]:
    """Powers the UsageCard chart. Window capped at 1-90 days here
    rather than at the route-arg level so the AdminPage can request
    1d/7d/30d ranges via the same endpoint."""
    if not (1 <= days <= 90):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="days must be between 1 and 90",
        )

    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func as sa_func
    from sqlalchemy import text as sa_text

    from finmind.models.billing import ApiUsageEvent

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)

    by_day_rows = (
        await db.execute(
            select(
                sa_func.strftime("%Y-%m-%d", ApiUsageEvent.ts).label("day")
                if db.bind.dialect.name == "sqlite"
                else sa_func.to_char(ApiUsageEvent.ts, "YYYY-MM-DD").label("day"),
                sa_func.count().label("calls"),
                sa_func.coalesce(
                    sa_func.sum(ApiUsageEvent.row_count), 0
                ).label("rows"),
            )
            .where(ApiUsageEvent.ts >= cutoff)
            .group_by(sa_text("day"))
            .order_by(sa_text("day"))
        )
    ).all()

    by_dataset_rows = (
        await db.execute(
            select(
                ApiUsageEvent.dataset_code,
                sa_func.count().label("calls"),
                sa_func.coalesce(
                    sa_func.sum(ApiUsageEvent.row_count), 0
                ).label("rows"),
            )
            .where(ApiUsageEvent.ts >= cutoff)
            .group_by(ApiUsageEvent.dataset_code)
            .order_by(sa_func.count().desc())
        )
    ).all()

    return {
        "window_days": days,
        "by_day": [
            {"day": r[0], "calls": int(r[1]), "rows": int(r[2])}
            for r in by_day_rows
        ],
        "by_dataset": [
            {
                "dataset_code": r[0] or "<unknown>",
                "calls": int(r[1]),
                "rows": int(r[2]),
            }
            for r in by_dataset_rows
        ],
    }
