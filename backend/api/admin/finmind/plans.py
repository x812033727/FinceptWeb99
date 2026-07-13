"""Pricing-plan management endpoints for the AdminPage FinMind proxy.

Covers listing, idempotent UPSERT (create-or-update), and soft-disable
of pricing plans.
"""
from __future__ import annotations

from fastapi import HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from ._shared import AdminUser, FmDb, _ensure_finmind_db_reachable, router


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
async def list_plans(_: AdminUser, db: FmDb) -> list[PlanItem]:
    await _ensure_finmind_db_reachable(db)
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
    code: str, body: PlanUpsert, _: AdminUser, db: FmDb,
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
    response_model=None,
    summary="AdminPage: disable a plan (soft — keeps subscriptions valid)",
)
async def disable_plan(code: str, _: AdminUser, db: FmDb) -> None:
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
