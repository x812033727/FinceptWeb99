"""User notification settings and provider callback endpoints.

Mounted at /api/notifications. The frontend SettingsPage toggle:
  1. GET  /vapid-public-key   → applicationServerKey for pushManager
  2. POST /push-subscribe     → persist the browser's subscription
  3. DELETE /push-subscribe   → remove it on toggle-off

Subscribe upserts by `endpoint` (globally unique — the push service
mints one URL per browser subscription): re-subscribing refreshes the
keys and, when a different account logs in on the same browser,
re-binds the row so notifications follow the signed-in user instead
of leaking to the previous one.

D2 adds owner-scoped Email/LINE preferences and a signed LINE webhook;
D3 owns the Web Push subscription surface above. Provider credentials
remain deployment secrets and never cross this API boundary.
"""
import base64
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_viewer
from config import settings
from db.session import get_db
from limiter import limiter
from models.push_subscription import PushSubscription
from models.notification_channel import NotificationChannel
from models.user import User
from services.channel_notification_service import (
    DEFAULT_EVENT_KINDS,
    hash_binding_token,
    provider_is_configured,
    send_channel_test,
)
from services.web_push_service import is_configured

from .schemas import (
    PushSubscribeIn,
    PushSubscriptionOut,
    PushUnsubscribeIn,
    VapidPublicKeyOut,
    ChannelTestOut,
    ChannelUpdateIn,
    LineBindingOut,
    NotificationChannelOut,
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


def _hint(kind: str, channel: NotificationChannel | None, email: str) -> str | None:
    if kind == "email":
        local, _, domain = email.partition("@")
        return f"{local[:2]}***@{domain}" if domain else "***"
    if channel is not None and channel.verified and channel.destination:
        return "LINE account connected"
    return None


def _channel_out(kind: str, channel: NotificationChannel | None, email: str) -> NotificationChannelOut:
    config = channel.config if channel is not None and channel.config else {}
    return NotificationChannelOut(
        kind=kind,
        enabled=bool(channel and channel.enabled),
        # The account email is already identity-bound by authentication;
        # it needs no second verification row before first enable.
        verified=kind == "email" or bool(channel and channel.verified),
        configured=provider_is_configured(kind),
        destination_hint=_hint(kind, channel, email),
        event_kinds=config.get("event_kinds", DEFAULT_EVENT_KINDS),
        daily_digest=bool(config.get("daily_digest", False)) if kind == "email" else False,
        failed_count=channel.failed_count if channel else 0,
        last_success_at=channel.last_success_at if channel else None,
    )


@router.get("/channels", response_model=list[NotificationChannelOut])
async def list_channels(user: CurrentUser, db: DB):
    uid = uuid.UUID(user["id"])
    rows = (await db.scalars(select(NotificationChannel).where(
        NotificationChannel.user_id == uid,
    ))).all()
    by_kind = {row.kind: row for row in rows}
    account = await db.get(User, uid)
    email = account.email if account else ""
    return [_channel_out(kind, by_kind.get(kind), email) for kind in ("email", "line")]


@router.put("/channels/{kind}", response_model=NotificationChannelOut)
@limiter.limit("30/minute")
async def update_channel(
    request: Request, kind: str, body: ChannelUpdateIn, user: CurrentUser, db: DB,
):
    if kind not in {"email", "line"}:
        raise HTTPException(404, "Unknown notification channel")
    uid = uuid.UUID(user["id"])
    channel = await db.scalar(select(NotificationChannel).where(
        NotificationChannel.user_id == uid,
        NotificationChannel.kind == kind,
    ))
    if channel is None:
        channel = NotificationChannel(user_id=uid, kind=kind)
        db.add(channel)
    if kind == "email":
        channel.verified = True
    if body.enabled or (kind == "email" and body.daily_digest):
        if not provider_is_configured(kind):
            raise HTTPException(409, f"{kind} provider is not configured")
        if kind == "line" and (not channel.verified or not channel.destination):
            raise HTTPException(409, "Connect a LINE account before enabling this channel")
    channel.enabled = body.enabled
    channel.config = {
        "event_kinds": body.event_kinds,
        "daily_digest": body.daily_digest if kind == "email" else False,
    }
    await db.commit()
    await db.refresh(channel)
    account = await db.get(User, uid)
    return _channel_out(kind, channel, account.email if account else "")


@router.delete("/channels/{kind}", status_code=204)
@limiter.limit("30/minute")
async def delete_channel(request: Request, kind: str, user: CurrentUser, db: DB):
    if kind not in {"email", "line"}:
        raise HTTPException(404, "Unknown notification channel")
    channel = await db.scalar(select(NotificationChannel).where(
        NotificationChannel.user_id == uuid.UUID(user["id"]),
        NotificationChannel.kind == kind,
    ))
    if channel is not None:
        await db.delete(channel)
        await db.commit()


@router.post("/channels/line/bind", response_model=LineBindingOut)
@limiter.limit("10/minute")
async def begin_line_binding(request: Request, user: CurrentUser, db: DB):
    if not provider_is_configured("line"):
        raise HTTPException(409, "LINE Messaging API is not configured")
    uid = uuid.UUID(user["id"])
    channel = await db.scalar(select(NotificationChannel).where(
        NotificationChannel.user_id == uid,
        NotificationChannel.kind == "line",
    ))
    if channel is None:
        channel = NotificationChannel(user_id=uid, kind="line", config={"event_kinds": DEFAULT_EVENT_KINDS})
        db.add(channel)
    token = secrets.token_urlsafe(18)
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    channel.binding_token_hash = hash_binding_token(token)
    channel.binding_expires_at = expires_at
    await db.commit()
    return LineBindingOut(
        token=token,
        expires_at=expires_at,
        instruction=f"Send this message to the Fincept LINE official account: FINCEPT {token}",
    )


@router.post("/channels/{kind}/test", response_model=ChannelTestOut)
@limiter.limit("5/minute")
async def test_channel(request: Request, kind: str, user: CurrentUser):
    if kind not in {"email", "line"}:
        raise HTTPException(404, "Unknown notification channel")
    delivered = await send_channel_test(user["id"], kind)
    if not delivered:
        raise HTTPException(409, "Channel is not ready or delivery failed")
    return ChannelTestOut(delivered=True)


def _line_signature_valid(body: bytes, signature: str) -> bool:
    secret = settings.LINE_CHANNEL_SECRET.encode("utf-8")
    digest = hmac.new(secret, body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)


@router.post("/line/webhook", include_in_schema=False)
@limiter.limit("120/minute")
async def line_webhook(request: Request, db: DB):
    """Signed LINE webhook. A user binds by messaging `FINCEPT <token>`.

    No access token or raw one-time token is logged or persisted. A valid
    token is consumed exactly once and expires after 15 minutes.
    """
    if not provider_is_configured("line"):
        raise HTTPException(503, "LINE Messaging API is not configured")
    raw = await request.body()
    signature = request.headers.get("x-line-signature", "")
    if not signature or not _line_signature_valid(raw, signature):
        raise HTTPException(401, "Invalid LINE signature")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("events", []), list):
        raise HTTPException(400, "Invalid LINE webhook payload")

    bound = 0
    for event in payload.get("events", []):
        if not isinstance(event, dict):
            continue
        message = event.get("message") or {}
        source = event.get("source") or {}
        if not isinstance(message, dict) or not isinstance(source, dict):
            continue
        if message.get("type") != "text" or not source.get("userId"):
            continue
        text = str(message.get("text") or "").strip()
        if not text.upper().startswith("FINCEPT "):
            continue
        token = text.split(maxsplit=1)[1].strip()
        channel = await db.scalar(
            select(NotificationChannel).where(
                NotificationChannel.binding_token_hash == hash_binding_token(token),
                NotificationChannel.kind == "line",
            ).with_for_update()
        )
        if channel is None or channel.binding_expires_at is None:
            continue
        expires_at = channel.binding_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at < datetime.now(UTC):
            channel.binding_token_hash = None
            channel.binding_expires_at = None
            continue
        # One LINE recipient belongs to at most one Fincept account. If it
        # is deliberately rebound, disable the old destination first so
        # alerts can never leak across accounts.
        previous = (await db.scalars(select(NotificationChannel).where(
            NotificationChannel.kind == "line",
            NotificationChannel.destination == source["userId"],
            NotificationChannel.id != channel.id,
        ))).all()
        for old in previous:
            old.enabled = False
            old.verified = False
            old.destination = None
        channel.destination = source["userId"]
        channel.verified = True
        channel.enabled = True
        channel.binding_token_hash = None
        channel.binding_expires_at = None
        bound += 1
    await db.commit()
    return {"ok": True, "bound": bound}
