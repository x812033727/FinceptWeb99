"""Opt-in Email and LINE Messaging API notification transports (D2).

Provider credentials stay in deployment configuration. User rows contain
only preferences and, for LINE, the opaque recipient id obtained through a
signed webhook plus a short-lived one-time binding token.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select

from config import settings
from db.session import AsyncSessionLocal
from middleware.metrics import NOTIFICATION_DELIVERIES_TOTAL
from models.notification_channel import NotificationChannel
from models.user import User
from services.email_service import is_configured as email_is_configured
from services.email_service import send_email

log = logging.getLogger(__name__)

DEFAULT_EVENT_KINDS = ["price_alert", "strategy_health"]
ALLOWED_EVENT_KINDS = frozenset(DEFAULT_EVENT_KINDS)
MAX_CONSECUTIVE_FAILURES = 5
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def line_is_configured() -> bool:
    return bool(settings.LINE_CHANNEL_ACCESS_TOKEN.strip()) and bool(
        settings.LINE_CHANNEL_SECRET.strip()
    )


def provider_is_configured(kind: str) -> bool:
    if kind == "email":
        return email_is_configured()
    if kind == "line":
        return line_is_configured()
    return False


def event_kind(payload: dict[str, Any]) -> str:
    if payload.get("kind") == "strategy_health_alert":
        return "strategy_health"
    if payload.get("type") == "alert":
        return "price_alert"
    return "system"


def _accepts(channel: NotificationChannel, payload: dict[str, Any]) -> bool:
    configured = channel.config or {}
    selected = configured.get("event_kinds", DEFAULT_EVENT_KINDS)
    return event_kind(payload) in selected


def _message(payload: dict[str, Any]) -> tuple[str, str]:
    symbol = str(payload.get("symbol") or "").strip()
    market = str(payload.get("market") or "").strip()
    if payload.get("kind") == "strategy_health_alert":
        title = "Fincept 策略健康告警"
    elif symbol:
        title = f"Fincept 告警｜{symbol}{f' ({market})' if market else ''}"
    else:
        title = "Fincept 通知"
    body = str(payload.get("message") or title).strip()
    return title, body


async def _eligible(user_id: str, kind: str, payload: dict[str, Any], *, force: bool = False):
    try:
        uid = uuid.UUID(str(user_id))
    except ValueError:
        return None, None, None
    db = AsyncSessionLocal()
    channel = await db.scalar(select(NotificationChannel).where(
        NotificationChannel.user_id == uid,
        NotificationChannel.kind == kind,
    ))
    if channel is None or not channel.verified or (not force and not channel.enabled):
        await db.close()
        return None, None, None
    if not force and not _accepts(channel, payload):
        NOTIFICATION_DELIVERIES_TOTAL.labels(channel=kind, outcome="filtered").inc()
        await db.close()
        return None, None, None
    user = await db.get(User, uid)
    if user is None:
        await db.close()
        return None, None, None
    return db, channel, user


async def _record_success(db, channel: NotificationChannel) -> None:
    channel.failed_count = 0
    channel.last_success_at = datetime.now(UTC)
    await db.commit()


async def _record_failure(db, channel: NotificationChannel) -> None:
    channel.failed_count = (channel.failed_count or 0) + 1
    if channel.failed_count >= MAX_CONSECUTIVE_FAILURES:
        channel.enabled = False
    await db.commit()


async def email_to_user(user_id: str, payload: dict[str, Any], *, force: bool = False) -> bool:
    """Notification transport. Returns delivery status for test endpoints."""
    if not email_is_configured():
        NOTIFICATION_DELIVERIES_TOTAL.labels(channel="email", outcome="unconfigured").inc()
        return False
    db, channel, user = await _eligible(user_id, "email", payload, force=force)
    if db is None:
        return False
    title, body = _message(payload)
    try:
        await send_email(to=user.email, subject=title, body_markdown=body)
    except Exception:
        await _record_failure(db, channel)
        NOTIFICATION_DELIVERIES_TOTAL.labels(channel="email", outcome="failed").inc()
        log.exception("email notification failed for user %s", user_id)
        return False
    else:
        await _record_success(db, channel)
        NOTIFICATION_DELIVERIES_TOTAL.labels(channel="email", outcome="sent").inc()
        return True
    finally:
        await db.close()


async def line_to_user(user_id: str, payload: dict[str, Any], *, force: bool = False) -> bool:
    """Push one text message with the deployment's LINE official account."""
    if not line_is_configured():
        NOTIFICATION_DELIVERIES_TOTAL.labels(channel="line", outcome="unconfigured").inc()
        return False
    db, channel, _user = await _eligible(user_id, "line", payload, force=force)
    if db is None or not channel.destination:
        if db is not None:
            await db.close()
        return False
    title, body = _message(payload)
    text = f"{title}\n{body}"[:5000]
    try:
        async with httpx.AsyncClient(timeout=settings.LINE_API_TIMEOUT_SECONDS) as client:
            response = await client.post(
                LINE_PUSH_URL,
                headers={
                    "Authorization": f"Bearer {settings.LINE_CHANNEL_ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={"to": channel.destination, "messages": [{"type": "text", "text": text}]},
            )
            response.raise_for_status()
    except Exception:
        await _record_failure(db, channel)
        NOTIFICATION_DELIVERIES_TOTAL.labels(channel="line", outcome="failed").inc()
        log.exception("LINE notification failed for user %s", user_id)
        return False
    else:
        await _record_success(db, channel)
        NOTIFICATION_DELIVERIES_TOTAL.labels(channel="line", outcome="sent").inc()
        return True
    finally:
        await db.close()


async def send_channel_test(user_id: str, kind: str) -> bool:
    payload = {"type": "alert", "symbol": "TEST", "market": "SYSTEM", "message": "Fincept 通知通道測試成功。"}
    if kind == "email":
        return await email_to_user(user_id, payload, force=True)
    if kind == "line":
        return await line_to_user(user_id, payload, force=True)
    return False


def hash_binding_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
