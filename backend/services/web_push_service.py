"""Web Push delivery (PR-D3 瀏覽器推播).

Registered as the "web_push" transport in `notification_service` at
startup (main.py + worker.py), so every `notify_user(...)` firing —
price alerts, strategy-health degradation — also lands as a browser
notification on each of the user's subscribed browsers, tab open or
not.

Design:
- Hard fail-closed gate via `is_configured()`: when either VAPID key
  env var is empty, `push_to_user` returns silently — same shape as
  `email_service.is_configured()`. Dev deployments without keys never
  crash the alert cron.
- pywebpush is synchronous (requests under the hood); each send runs
  in `asyncio.to_thread(...)` so the event loop is never blocked —
  the aiosmtplib-style non-blocking contract of the transport seam.
- Subscription hygiene: HTTP 404/410 from the push service mean the
  subscription is gone (user revoked permission / browser rotated the
  endpoint) → delete the row. Any other failure increments
  `failed_count`; after MAX_CONSECUTIVE_FAILURES the row is pruned so
  a dead endpoint doesn't burn a request per alert forever. A success
  resets the counter and stamps `last_success_at`.

Generate keys once per deployment (see .env.example):
    vapid --gen        # from py-vapid, installed with pywebpush
or via the openssl/python one-liners documented there.
"""
import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime

from pywebpush import WebPushException, webpush
from sqlalchemy import select

from config import settings
from db.session import AsyncSessionLocal
from models.push_subscription import PushSubscription

log = logging.getLogger(__name__)

# Prune a subscription after this many CONSECUTIVE failed deliveries
# (404/410 prunes immediately regardless).
MAX_CONSECUTIVE_FAILURES = 5

# Per-request timeout for the push-service POST. Keeps one slow push
# service from monopolizing the default thread pool.
PUSH_TIMEOUT_SECONDS = 10

# Push-service response codes that mean "this subscription no longer
# exists" — prune immediately.
_GONE_STATUS = {404, 410}


def is_configured() -> bool:
    """True iff both VAPID keys are set. Fail-closed like
    `email_service.is_configured()` — unset config means the transport
    skips silently rather than erroring per alert."""
    return bool(settings.VAPID_PUBLIC_KEY.strip()) and bool(
        settings.VAPID_PRIVATE_KEY.strip()
    )


def _vapid_claims() -> dict:
    """Fresh claims dict per send — pywebpush mutates it in place
    (adds aud/exp), so sharing one dict across calls poisons the
    audience of every subsequent push."""
    email = settings.VAPID_CLAIMS_EMAIL.strip() or "admin@example.com"
    return {"sub": f"mailto:{email}"}


def _notification_payload(payload: dict) -> str:
    """Map an internal alert payload onto the JSON contract the
    service worker's `push` handler renders (title/body/tag/url)."""
    symbol = payload.get("symbol") or ""
    market = payload.get("market") or ""
    kind = payload.get("type") or "alert"
    title = f"{symbol} ({market}) 告警" if symbol else "Fincept 通知"
    return json.dumps({
        "title": title,
        "body": payload.get("message") or "",
        # One tag per alert rule: browsers collapse re-fires of the
        # same repeating alert instead of stacking notifications.
        "tag": f"fincept-{kind}-{payload.get('id') or symbol or 'generic'}",
        "url": "/alerts",
    }, ensure_ascii=False)


def _send_one(endpoint: str, keys: dict, data: str) -> None:
    """Synchronous single-subscription send — runs inside
    asyncio.to_thread. Raises WebPushException on any non-2xx."""
    webpush(
        subscription_info={"endpoint": endpoint, "keys": keys},
        data=data,
        vapid_private_key=settings.VAPID_PRIVATE_KEY,
        vapid_claims=_vapid_claims(),
        ttl=3600,
        timeout=PUSH_TIMEOUT_SECONDS,
    )


async def push_to_user(user_id: str, payload: dict) -> None:
    """Transport entry point (registered as "web_push").

    Sends the alert to every subscription the user holds. Never raises
    for delivery failures — bookkeeping (prune / failed_count) is the
    error handling; `notification_service` additionally isolates any
    unexpected exception so the websocket transport is never affected.
    """
    if not is_configured():
        return

    try:
        uid = uuid.UUID(str(user_id))
    except ValueError:
        return

    data = _notification_payload(payload)

    async with AsyncSessionLocal() as db:
        subs = (await db.execute(
            select(PushSubscription).where(PushSubscription.user_id == uid)
        )).scalars().all()

        for sub in subs:
            try:
                await asyncio.to_thread(_send_one, sub.endpoint, sub.keys, data)
            except WebPushException as exc:
                status = getattr(exc.response, "status_code", None)
                if status in _GONE_STATUS:
                    # Subscription revoked/expired at the push service.
                    log.info("web push subscription gone (HTTP %s), pruning %s",
                             status, sub.id)
                    await db.delete(sub)
                else:
                    sub.failed_count = (sub.failed_count or 0) + 1
                    if sub.failed_count >= MAX_CONSECUTIVE_FAILURES:
                        log.warning(
                            "web push subscription %s failed %d times, pruning",
                            sub.id, sub.failed_count,
                        )
                        await db.delete(sub)
                    else:
                        log.warning("web push delivery failed for %s: %s",
                                    sub.id, exc)
            except Exception:
                # Defensive: pywebpush can surface raw requests errors
                # (DNS failure, timeout) — same increment-and-prune path.
                sub.failed_count = (sub.failed_count or 0) + 1
                if sub.failed_count >= MAX_CONSECUTIVE_FAILURES:
                    await db.delete(sub)
                log.exception("web push delivery error for %s", sub.id)
            else:
                sub.last_success_at = datetime.now(UTC)
                sub.failed_count = 0

        await db.commit()
