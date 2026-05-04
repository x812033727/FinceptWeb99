"""Stripe webhook receiver — signature verification + event dispatch.

Purposefully written WITHOUT the `stripe` SDK dependency. Stripe's
webhook signature scheme is small enough to implement directly with
`hmac` + `hashlib`, and the events we care about (subscription created,
updated, canceled, payment_failed) are JSON dicts we can route on
event.type without object hydration.

Operational shape:

  POST /api/finmind/webhooks/stripe
    Header: Stripe-Signature: t=<ts>,v1=<sig>[,v0=<sig>]
    Body:   stripe Event JSON

  → verify signature against `STRIPE_WEBHOOK_SECRET` env var
  → INSERT into `payment_events` (UNIQUE (provider, event_id) makes
    re-delivery a no-op — Stripe retries failed deliveries up to 3
    days, so dedup is not optional)
  → process_event(): switch on event.type, mutate subscriptions

Event types handled in this module:
  - customer.subscription.created  → INSERT into subscriptions
                                     (status='active', plan from
                                     metadata.plan_code)
  - customer.subscription.updated  → UPDATE plan / status / expires_at
  - customer.subscription.deleted  → status='canceled', expires_at=NOW()
  - invoice.payment_failed         → status='past_due'

Anything else is recorded in payment_events but not acted on — the
inbox is the source of truth, the dispatcher just translates known
event types into subscription state.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from finmind.models.billing import PaymentEvent, Subscription

log = logging.getLogger("finmind.stripe_webhook")

# How far we accept clock skew between Stripe's signing timestamp and
# our wall clock. Stripe recommends 5 minutes — anything older is
# almost certainly a replay attack.
SIGNATURE_TOLERANCE_SECONDS = 5 * 60


class SignatureError(Exception):
    """Raised when the X-Stripe-Signature header is malformed, doesn't
    verify against `secret`, or is older than the tolerance window.
    Caller maps to HTTP 401 — never reveal which check failed (avoids
    oracle attacks)."""


@dataclass
class WebhookOutcome:
    event_id: str
    event_type: str
    status: str           # 'processed' | 'duplicate' | 'unhandled'
    note: str | None = None


def _parse_signature_header(header: str) -> tuple[int, list[str]]:
    """Parse `t=<ts>,v1=<sig>[,v1=<sig>]` into (timestamp, [sig, ...]).
    Stripe sometimes lists multiple v1 signatures (e.g. during a
    secret rotation) — we accept any match.
    """
    timestamp: int | None = None
    sigs: list[str] = []
    for part in header.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k == "t":
            try:
                timestamp = int(v)
            except ValueError:
                pass
        elif k == "v1":
            sigs.append(v)
    if timestamp is None or not sigs:
        raise SignatureError("malformed Stripe-Signature header")
    return timestamp, sigs


def verify_signature(
    payload: bytes, signature_header: str, secret: str,
    *, now: int | None = None,
) -> None:
    """Raise SignatureError on mismatch / stale / malformed.

    Signing scheme: HMAC-SHA256(f"{timestamp}.{payload}", secret) hex.
    Comparison via `hmac.compare_digest` to dodge timing attacks.
    """
    if not secret:
        raise SignatureError("STRIPE_WEBHOOK_SECRET not configured")

    ts, signatures = _parse_signature_header(signature_header)

    actual_now = int(time.time()) if now is None else now
    if abs(actual_now - ts) > SIGNATURE_TOLERANCE_SECONDS:
        raise SignatureError("signature timestamp outside tolerance window")

    signed_payload = f"{ts}.".encode() + payload
    expected = hmac.new(
        secret.encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()

    for sig in signatures:
        if hmac.compare_digest(expected, sig):
            return
    raise SignatureError("no signature matched")


# ── Event dispatch ──────────────────────────────────────────────


def _event_id(event: dict[str, Any]) -> str | None:
    return event.get("id")


def _event_type(event: dict[str, Any]) -> str:
    return event.get("type") or ""


def _to_date(epoch: int | None) -> date | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).date()
    except (ValueError, TypeError, OSError):
        return None


def _customer_email(sub_obj: dict[str, Any]) -> str | None:
    """Stripe carries the customer email inconsistently. Walk the most
    common locations; metadata.owner_email is what we ask the operator
    to set in their Stripe Checkout configuration."""
    md = sub_obj.get("metadata") or {}
    return (
        md.get("owner_email")
        or sub_obj.get("customer_email")
        or (sub_obj.get("customer") or {}).get("email")
        if isinstance(sub_obj.get("customer"), dict)
        else (
            md.get("owner_email")
            or sub_obj.get("customer_email")
        )
    )


def _plan_code(sub_obj: dict[str, Any]) -> str | None:
    """Per the design, plan code lives in subscription.metadata.plan_code
    (operators set this when creating the Stripe Price). Fall back to
    the price's nickname if metadata absent."""
    md = sub_obj.get("metadata") or {}
    if md.get("plan_code"):
        return md["plan_code"]
    items = (sub_obj.get("items") or {}).get("data") or []
    if items:
        price = items[0].get("price") or {}
        return price.get("nickname") or price.get("lookup_key")
    return None


async def _record_event(
    session: AsyncSession, event: dict[str, Any]
) -> bool:
    """INSERT into payment_events with ON CONFLICT DO NOTHING for
    dedup. Returns True if this is a new event, False if Stripe
    re-delivered something we already saw."""
    event_id = _event_id(event)
    if not event_id:
        # Bare malformed payload — log and move on. Caller still
        # returns 200 to stop Stripe retrying a bad event forever.
        return False

    payload = {
        "provider": "stripe",
        "event_id": event_id,
        "event_type": _event_type(event),
        "payload": event,
    }
    dialect = session.bind.dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = insert_fn(PaymentEvent).values(**payload)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["provider", "event_id"]
    )
    result = await session.execute(stmt)
    await session.commit()
    # rowcount > 0 = inserted; 0 = duplicate (skipped by ON CONFLICT).
    return (result.rowcount or 0) > 0


async def _mark_processed(
    session: AsyncSession, event_id: str, error: str | None = None
) -> None:
    await session.execute(
        update(PaymentEvent)
        .where(
            PaymentEvent.provider == "stripe",
            PaymentEvent.event_id == event_id,
        )
        .values(
            processed_at=datetime.now(tz=timezone.utc),
            error=error,
        )
    )
    await session.commit()


async def _handle_subscription_created(
    session: AsyncSession, sub_obj: dict[str, Any]
) -> str:
    email = _customer_email(sub_obj)
    plan_code = _plan_code(sub_obj)
    sub_id = sub_obj.get("id")
    if not (email and plan_code and sub_id):
        return f"missing email / plan_code / id (have {bool(email)} / {bool(plan_code)} / {bool(sub_id)})"

    started_epoch = sub_obj.get("start_date") or sub_obj.get("current_period_start")
    started_at = _to_date(started_epoch) or datetime.now(tz=timezone.utc).date()
    expires_at = _to_date(sub_obj.get("current_period_end"))

    # Idempotency: same external_sub_id = same row. If Stripe re-sends
    # `created` after a subscription already exists locally (e.g. operator
    # cleared a test subscription), update rather than duplicate.
    existing = (
        await session.execute(
            select(Subscription).where(
                Subscription.external_provider == "stripe",
                Subscription.external_sub_id == sub_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.plan_code = plan_code
        existing.status = "active"
        existing.started_at = started_at
        existing.expires_at = expires_at
        await session.commit()
        return "updated existing subscription"

    session.add(Subscription(
        owner_email=email,
        plan_code=plan_code,
        status="active",
        started_at=started_at,
        expires_at=expires_at,
        external_provider="stripe",
        external_sub_id=sub_id,
        auto_renew=True,
    ))
    await session.commit()
    return "subscription created"


async def _handle_subscription_updated(
    session: AsyncSession, sub_obj: dict[str, Any]
) -> str:
    sub_id = sub_obj.get("id")
    if not sub_id:
        return "missing id"

    existing = (
        await session.execute(
            select(Subscription).where(
                Subscription.external_provider == "stripe",
                Subscription.external_sub_id == sub_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        # Stripe sometimes sends `updated` before we've processed
        # `created` (out-of-order delivery). Fall back to insert.
        return await _handle_subscription_created(session, sub_obj)

    plan_code = _plan_code(sub_obj)
    if plan_code:
        existing.plan_code = plan_code
    # Stripe.status: 'active' | 'past_due' | 'canceled' | 'unpaid' |
    # 'incomplete' | 'trialing' — map to our subset.
    stripe_status = sub_obj.get("status") or "active"
    existing.status = {
        "trialing": "trial",
        "active": "active",
        "past_due": "past_due",
        "unpaid": "past_due",
        "canceled": "canceled",
        "incomplete": "past_due",
        "incomplete_expired": "expired",
    }.get(stripe_status, stripe_status)
    new_expires = _to_date(sub_obj.get("current_period_end"))
    if new_expires is not None:
        existing.expires_at = new_expires
    await session.commit()
    return "subscription updated"


async def _handle_subscription_deleted(
    session: AsyncSession, sub_obj: dict[str, Any]
) -> str:
    sub_id = sub_obj.get("id")
    if not sub_id:
        return "missing id"
    await session.execute(
        update(Subscription)
        .where(
            Subscription.external_provider == "stripe",
            Subscription.external_sub_id == sub_id,
        )
        .values(
            status="canceled",
            expires_at=datetime.now(tz=timezone.utc).date(),
        )
    )
    await session.commit()
    return "subscription canceled"


async def _handle_invoice_payment_failed(
    session: AsyncSession, invoice: dict[str, Any]
) -> str:
    sub_id = invoice.get("subscription")
    if not sub_id:
        return "invoice not tied to a subscription"
    await session.execute(
        update(Subscription)
        .where(
            Subscription.external_provider == "stripe",
            Subscription.external_sub_id == sub_id,
        )
        .values(status="past_due")
    )
    await session.commit()
    return "marked past_due"


# Switch table — keeps the dispatch readable and easy to extend.
_HANDLERS = {
    "customer.subscription.created":  _handle_subscription_created,
    "customer.subscription.updated":  _handle_subscription_updated,
    "customer.subscription.deleted":  _handle_subscription_deleted,
    "invoice.payment_failed":         _handle_invoice_payment_failed,
}


async def process_event(
    session: AsyncSession, event: dict[str, Any]
) -> WebhookOutcome:
    """Record + dispatch one Stripe event. Caller must verify the
    signature before invoking this — `process_event` trusts its input."""
    event_id = _event_id(event) or "<no-id>"
    event_type = _event_type(event)

    inserted = await _record_event(session, event)
    if not inserted:
        return WebhookOutcome(event_id, event_type, "duplicate")

    handler = _HANDLERS.get(event_type)
    if handler is None:
        await _mark_processed(session, event_id, error=None)
        return WebhookOutcome(
            event_id, event_type, "unhandled",
            note="event recorded; no handler registered for this type",
        )

    obj = (event.get("data") or {}).get("object") or {}
    try:
        note = await handler(session, obj)
    except Exception as exc:
        log.exception("stripe webhook handler crashed: %s", event_type)
        await _mark_processed(session, event_id, error=repr(exc))
        # Re-raise so the caller can return 500 → Stripe retries.
        raise

    await _mark_processed(session, event_id, error=None)
    return WebhookOutcome(event_id, event_type, "processed", note=note)
