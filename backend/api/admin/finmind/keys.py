"""API key management endpoints for the AdminPage FinMind proxy.

Covers issuing a new key (with optional plan-linked subscription),
listing keys, and soft-revoking a key.
"""
from __future__ import annotations

from fastapi import HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from ._shared import AdminUser, FmDb, _ensure_finmind_db_reachable, router


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
    body: IssueKeyRequest, _: AdminUser, db: FmDb,
) -> IssuedKeyResponse:
    """Generates a fresh `fck_live_<prefix><suffix>` key, persists
    sha256 + prefix only, and returns the plaintext for one-time
    display.

    When `plan_code` is supplied AND a matching enabled Plan exists,
    creates a Subscription (status='active', started_at=today) and
    links the new ApiKey to it. The customer's quota then comes
    from `plans.quota_daily_*` instead of free-tier defaults.

    Unknown / disabled plan_code → 400. The previous silent fallback
    (issue free-tier key, surface plan_code=None in the response)
    masked typos and disabled-plan oversights — the operator's intent
    when they typed a plan_code was clearly to assign that plan, so
    surfacing the mismatch loudly is the safer default."""
    from datetime import date

    from finmind.billing.keys import issue_key
    from finmind.models.billing import Plan, Subscription

    subscription_id: int | None = None
    resolved_plan_code: str | None = None
    if body.plan_code:
        plan = await db.get(Plan, body.plan_code)
        if plan is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"plan_code {body.plan_code!r} does not exist. "
                    "Create the plan first via /admin/finmind/plans, "
                    "or omit plan_code to issue a free-tier key."
                ),
            )
        if not plan.enabled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"plan_code {body.plan_code!r} is disabled. "
                    "Re-enable the plan or pick a different one."
                ),
            )
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
async def list_finmind_keys(_: AdminUser, db: FmDb) -> list[ApiKeyItem]:
    """Joins api_keys → subscriptions to surface plan_code per row.
    Free-tier keys (no subscription) show plan_code=None — frontend
    renders these with a muted "free" badge."""
    await _ensure_finmind_db_reachable(db)
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
    response_model=None,
    summary="AdminPage: disable a key (soft-revoke; keeps audit trail)",
)
async def revoke_finmind_key(key_id: int, _: AdminUser, db: FmDb) -> None:
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
