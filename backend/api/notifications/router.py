"""Web Push subscription endpoints (PR-D3 瀏覽器推播).

Mounted at /api/notifications. The frontend SettingsPage toggle:
  1. GET  /vapid-public-key   → applicationServerKey for pushManager
  2. POST /push-subscribe     → persist the browser's subscription
  3. DELETE /push-subscribe   → remove it on toggle-off

Subscribe upserts by `endpoint` (globally unique — the push service
mints one URL per browser subscription): re-subscribing refreshes the
keys and, when a different account logs in on the same browser,
re-binds the row so notifications follow the signed-in user instead
of leaking to the previous one.
"""
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_viewer
from config import settings
from db.session import get_db
from limiter import limiter
from models.push_subscription import PushSubscription
from services.web_push_service import is_configured

from .schemas import (
    PushSubscribeIn,
    PushSubscriptionOut,
    PushUnsubscribeIn,
    VapidPublicKeyOut,
)

router = APIRouter()
CurrentUser = Annotated[dict, Depends(require_viewer)]
DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("/vapid-public-key", response_model=VapidPublicKeyOut)
async def vapid_public_key(user: CurrentUser):
    """Application server key for `pushManager.subscribe`. `configured`
    false (public_key null) when the deployment has no VAPID keys."""
    configured = is_configured()
    return VapidPublicKeyOut(
        configured=configured,
        public_key=settings.VAPID_PUBLIC_KEY.strip() if configured else None,
    )


@router.post("/push-subscribe", response_model=PushSubscriptionOut, status_code=201)
@limiter.limit("30/minute")
async def push_subscribe(
    request: Request, body: PushSubscribeIn, user: CurrentUser, db: DB,
):
    """Upsert by endpoint: refreshes keys / rebinds owner on conflict
    instead of 409ing — the browser is the source of truth for what
    subscription it currently holds."""
    existing = await db.scalar(
        select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)
    )
    if existing is not None:
        existing.user_id = uuid.UUID(user["id"])
        existing.keys = body.keys.model_dump()
        existing.user_agent = body.user_agent
        existing.failed_count = 0
        await db.commit()
        await db.refresh(existing)
        return existing

    sub = PushSubscription(
        user_id=uuid.UUID(user["id"]),
        endpoint=body.endpoint,
        keys=body.keys.model_dump(),
        user_agent=body.user_agent,
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return sub


@router.delete("/push-subscribe", status_code=204)
@limiter.limit("30/minute")
async def push_unsubscribe(
    request: Request, body: PushUnsubscribeIn, user: CurrentUser, db: DB,
):
    """User-scoped delete: you can only remove your own subscription
    (an endpoint bound to another account 404s rather than leaking
    its existence via 403)."""
    sub = await db.scalar(
        select(PushSubscription).where(
            PushSubscription.endpoint == body.endpoint,
            PushSubscription.user_id == uuid.UUID(user["id"]),
        )
    )
    if sub is None:
        raise HTTPException(404, "Subscription not found")
    await db.delete(sub)
    await db.commit()
