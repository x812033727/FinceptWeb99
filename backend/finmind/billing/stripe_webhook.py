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
  - customer.subscription.created  → INSERT into subscriptions with
                                     Stripe's actual status (an initial
                                     subscription can be incomplete)
  - customer.subscription.updated  → UPDATE plan / status / expires_at
  - customer.subscription.deleted  → status='canceled', expires_at=NOW()
  - invoice.paid                   → status='active'
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
from datetime import UTC, date, datetime
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
    """Raised when a Stripe webhook fails signature verification.

    `category` distinguishes the failure mode for OPS log triage —
    `secret_unset` means deploy misconfig (we can't accept anything),
    `malformed_header` means the header wasn't Stripe-format (bot
    probe?), `stale_timestamp` means replay attack window exceeded,
    `signature_mismatch` means wrong secret or tampered payload. The
    HTTP response stays generic 401 to avoid being an oracle."""

    def __init__(self, message: str, category: str = "unknown"):
        super().__init__(message)
        self.category = category


@dataclass
class WebhookOutcome:
    event_id: str
    event_type: str
    status: str  # 'processed' | 'duplicate' | 'unhandled'
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
        raise SignatureError(
            "malformed Stripe-Signature header",
            category="malformed_header",
        )
    return timestamp, sigs


def verify_signature(
    payload: bytes,
    signature_header: str,
    secret: str,
    *,
    now: int | None = None,
) -> None:
    """Raise SignatureError on mismatch / stale / malformed.

    Signing scheme: HMAC-SHA256(f"{timestamp}.{payload}", secret) hex.
    Comparison via `hmac.compare_digest` to dodge timing attacks.
    """
    if not secret:
        raise SignatureError(
            "STRIPE_WEBHOOK_SECRET not configured",
            category="secret_unset",
        )

    ts, signatures = _parse_signature_header(signature_header)

    actual_now = int(time.time()) if now is None else now
    if abs(actual_now - ts) > SIGNATURE_TOLERANCE_SECONDS:
        raise SignatureError(
            "signature timestamp outside tolerance window",
            category="stale_timestamp",
        )

    signed_payload = f"{ts}.".encode() + payload
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()

    for sig in signatures:
        if hmac.compare_digest(expected, sig):
            return
    raise SignatureError(
        "no signature matched",
        category="signature_mismatch",
    )


# ── Event dispatch ──────────────────────────────────────────────


def _event_id(event: dict[str, Any]) -> str | None:
    return event.get("id")


def _event_type(event: dict[str, Any]) -> str:
    return event.get("type") or ""


def _to_date(epoch: int | None) -> date | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=UTC).date()
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
        else (md.get("owner_email") or sub_obj.get("customer_email"))
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


def _subscription_status(stripe_status: Any) -> str:
    """Map Stripe's lifecycle to the local access-control states.

    Only ``active`` and ``trial`` grant quota access. New or unknown Stripe
    states therefore map to ``pending`` instead of optimistically enabling a
    subscription. In particular, Stripe documents that a newly-created
    subscription can be ``incomplete`` while its first payment still needs
    customer action.
    """
    return {
        "trialing": "trial",
        "active": "active",
        "past_due": "past_due",
        "unpaid": "past_due",
        "canceled": "canceled",
        "incomplete": "pending",
        "incomplete_expired": "expired",
        "paused": "paused",
    }.get(str(stripe_status or "").lower(), "pending")


def _event_created(event: dict[str, Any]) -> int | None:
    try:
        created = int(event.get("created"))
    except (TypeError, ValueError):
        return None
    return created if created >= 0 else None


def _event_is_stale(subscription: Subscription, event_created: int | None) -> bool:
    previous = subscription.last_external_event_created_at
    return event_created is not None and previous is not None and event_created < previous


def _event_would_relax_same_second(
    subscription: Subscription,
    event_created: int | None,
    incoming_status: str,
) -> bool:
    """Prefer access-restricting state when Stripe timestamps tie."""
    previous = subscription.last_external_event_created_at
    granting = {"trial", "active"}
    return (
        event_created is not None
        and previous == event_created
        and subscription.status not in granting
        and incoming_status in granting
    )


def _stamp_event(subscription: Subscription, event_created: int | None) -> None:
    if event_created is not None:
        subscription.last_external_event_created_at = event_created


async def _record_event(session: AsyncSession, event: dict[str, Any]) -> str:
    """INSERT into payment_events with ON CONFLICT DO NOTHING for
    dedup. Returns ``new``, ``duplicate``, or ``retry``. A prior handler
    failure remains retryable because Stripe redelivery is how out-of-order
    subscription events recover."""
    event_id = _event_id(event)
    if not event_id:
        # Bare malformed payload — log and move on. Caller still
        # returns 200 to stop Stripe retrying a bad event forever.
        return "duplicate"

    payload = {
        "provider": "stripe",
        "event_id": event_id,
        "event_type": _event_type(event),
        "payload": event,
    }
    dialect = session.bind.dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = insert_fn(PaymentEvent).values(**payload)
    stmt = stmt.on_conflict_do_nothing(index_elements=["provider", "event_id"])
    result = await session.execute(stmt)
    await session.commit()
    if (result.rowcount or 0) > 0:
        return "new"

    recorded = await session.scalar(
        select(PaymentEvent).where(
            PaymentEvent.provider == "stripe",
            PaymentEvent.event_id == event_id,
        )
    )
    if recorded is not None and recorded.processed_at is not None:
        return "duplicate"
    if recorded is not None and recorded.error is not None:
        return "retry"
    return "processing"


async def _mark_processed(session: AsyncSession, event_id: str, error: str | None = None) -> None:
    await session.execute(
        update(PaymentEvent)
        .where(
            PaymentEvent.provider == "stripe",
            PaymentEvent.event_id == event_id,
        )
        .values(
            processed_at=None if error is not None else datetime.now(tz=UTC),
            error=error,
        )
    )
    await session.commit()


async def _handle_subscription_created(
    session: AsyncSession,
    sub_obj: dict[str, Any],
    event_created: int | None,
) -> str:
    email = _customer_email(sub_obj)
    plan_code = _plan_code(sub_obj)
    sub_id = sub_obj.get("id")
    if not (email and plan_code and sub_id):
        return f"missing email / plan_code / id (have {bool(email)} / {bool(plan_code)} / {bool(sub_id)})"

    started_epoch = sub_obj.get("start_date") or sub_obj.get("current_period_start")
    started_at = _to_date(started_epoch) or datetime.now(tz=UTC).date()
    expires_at = _to_date(sub_obj.get("current_period_end"))
    status = _subscription_status(sub_obj.get("status"))

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
        if _event_is_stale(existing, event_created):
            return "ignored stale subscription.created"
        if _event_would_relax_same_second(existing, event_created, status):
            return "ignored same-second access relaxation"
        existing.plan_code = plan_code
        existing.status = status
        existing.started_at = started_at
        existing.expires_at = expires_at
        _stamp_event(existing, event_created)
        await session.commit()
        return "updated existing subscription"

    session.add(
        Subscription(
            owner_email=email,
            plan_code=plan_code,
            status=status,
            started_at=started_at,
            expires_at=expires_at,
            external_provider="stripe",
            external_sub_id=sub_id,
            last_external_event_created_at=event_created,
            auto_renew=True,
        )
    )
    await session.commit()
    return "subscription created"


async def _handle_subscription_updated(
    session: AsyncSession,
    sub_obj: dict[str, Any],
    event_created: int | None,
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
        return await _handle_subscription_created(session, sub_obj, event_created)

    if _event_is_stale(existing, event_created):
        return "ignored stale subscription.updated"

    plan_code = _plan_code(sub_obj)
    if plan_code:
        existing.plan_code = plan_code
    status = _subscription_status(sub_obj.get("status"))
    if _event_would_relax_same_second(existing, event_created, status):
        return "ignored same-second access relaxation"
    existing.status = status
    new_expires = _to_date(sub_obj.get("current_period_end"))
    if new_expires is not None:
        existing.expires_at = new_expires
    _stamp_event(existing, event_created)
    await session.commit()
    return "subscription updated"


async def _handle_subscription_deleted(
    session: AsyncSession,
    sub_obj: dict[str, Any],
    event_created: int | None,
) -> str:
    sub_id = sub_obj.get("id")
    if not sub_id:
        return "missing id"
    existing = await session.scalar(
        select(Subscription).where(
            Subscription.external_provider == "stripe",
            Subscription.external_sub_id == sub_id,
        )
    )
    if existing is None:
        return "subscription not yet recorded"
    if _event_is_stale(existing, event_created):
        return "ignored stale subscription.deleted"
    existing.status = "canceled"
    existing.expires_at = datetime.now(tz=UTC).date()
    _stamp_event(existing, event_created)
    await session.commit()
    return "subscription canceled"


async def _handle_invoice_payment_failed(
    session: AsyncSession,
    invoice: dict[str, Any],
    event_created: int | None,
) -> str:
    sub_ref = invoice.get("subscription")
    sub_id = sub_ref.get("id") if isinstance(sub_ref, dict) else sub_ref
    if not sub_id:
        return "invoice not tied to a subscription"
    existing = await session.scalar(
        select(Subscription).where(
            Subscription.external_provider == "stripe",
            Subscription.external_sub_id == sub_id,
        )
    )
    if existing is None:
        raise RuntimeError(f"subscription {sub_id} not yet recorded")
    if _event_is_stale(existing, event_created):
        return "ignored stale invoice.payment_failed"
    if existing.status == "pending":
        _stamp_event(existing, event_created)
        await session.commit()
        return "initial payment failure remains pending"
    existing.status = "past_due"
    _stamp_event(existing, event_created)
    await session.commit()
    return "marked past_due"


async def _handle_invoice_paid(
    session: AsyncSession,
    invoice: dict[str, Any],
    event_created: int | None,
) -> str:
    sub_ref = invoice.get("subscription")
    sub_id = sub_ref.get("id") if isinstance(sub_ref, dict) else sub_ref
    if not sub_id:
        return "invoice not tied to a subscription"
    existing = await session.scalar(
        select(Subscription).where(
            Subscription.external_provider == "stripe",
            Subscription.external_sub_id == sub_id,
        )
    )
    if existing is None:
        raise RuntimeError(f"subscription {sub_id} not yet recorded")
    if _event_is_stale(existing, event_created):
        return "ignored stale invoice.paid"
    if existing.status in {"canceled", "expired", "paused"}:
        return f"ignored invoice.paid for {existing.status} subscription"
    if _event_would_relax_same_second(existing, event_created, "active"):
        return "ignored same-second access relaxation"
    existing.status = "active"
    _stamp_event(existing, event_created)
    await session.commit()
    return "marked active"


# Switch table — keeps the dispatch readable and easy to extend.
_HANDLERS = {
    "customer.subscription.created": _handle_subscription_created,
    "customer.subscription.updated": _handle_subscription_updated,
    "customer.subscription.deleted": _handle_subscription_deleted,
    "invoice.paid": _handle_invoice_paid,
    "invoice.payment_failed": _handle_invoice_payment_failed,
}


async def process_event(session: AsyncSession, event: dict[str, Any]) -> WebhookOutcome:
    """Record + dispatch one Stripe event. Caller must verify the
    signature before invoking this — `process_event` trusts its input."""
    event_id = _event_id(event) or "<no-id>"
    event_type = _event_type(event)

    record_status = await _record_event(session, event)
    if record_status == "duplicate":
        return WebhookOutcome(event_id, event_type, "duplicate")
    if record_status == "processing":
        raise RuntimeError(f"event {event_id} is already being processed")

    handler = _HANDLERS.get(event_type)
    if handler is None:
        await _mark_processed(session, event_id, error=None)
        return WebhookOutcome(
            event_id,
            event_type,
            "unhandled",
            note="event recorded; no handler registered for this type",
        )

    obj = (event.get("data") or {}).get("object") or {}
    try:
        note = await handler(session, obj, _event_created(event))
    except Exception as exc:
        # Surface enough context for ops to manually audit the
        # affected customer when the automated path crashes —
        # `event_id` alone isn't human-debuggable, but with the
        # subscription id + customer email a support engineer can
        # cross-reference Stripe dashboard immediately.
        sub_id = obj.get("id") if isinstance(obj, dict) else None
        customer_email = _customer_email(obj) if isinstance(obj, dict) else None
        log.exception(
            "stripe webhook handler crashed: type=%s event_id=%s "
            "stripe_object_id=%s customer_email=%s",
            event_type,
            event_id,
            sub_id,
            customer_email,
        )
        await _mark_processed(session, event_id, error=repr(exc))
        # Re-raise so the caller can return 500 → Stripe retries.
        raise

    await _mark_processed(session, event_id, error=None)
    return WebhookOutcome(event_id, event_type, "processed", note=note)
