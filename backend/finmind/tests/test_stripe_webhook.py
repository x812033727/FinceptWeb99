"""Tests for `finmind.billing.stripe_webhook`.

Covers:
  - Signature verification: valid, malformed header, stale timestamp,
    rotation (multiple v1 sigs), wrong secret
  - Event recording is idempotent (Stripe re-deliveries dedup)
  - Subscription lifecycle: created → updated → deleted → past_due
  - Out-of-order delivery (`updated` arrives before `created`) handled
  - End-to-end POST /webhooks/stripe — 200 on valid sig, 401 on bad
    sig, 503 when secret unset
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from finmind.api.router import router
from finmind.billing.stripe_webhook import (
    SIGNATURE_TOLERANCE_SECONDS,
    SignatureError,
    process_event,
    verify_signature,
)
from finmind.db.session import get_finmind_db
from finmind.models.billing import PaymentEvent, Subscription


_TEST_SECRET = "whsec_test_dontuse_for_real"


def _sign(payload: bytes, secret: str = _TEST_SECRET, ts: int | None = None) -> str:
    """Build a Stripe-style signature header for tests."""
    ts = int(time.time()) if ts is None else ts
    signed = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


# ── Signature verification ─────────────────────────────────────


def test_verify_signature_accepts_fresh_valid():
    payload = b'{"id":"evt_1","type":"customer.subscription.created"}'
    header = _sign(payload)
    verify_signature(payload, header, _TEST_SECRET)  # no raise


def test_verify_signature_rejects_wrong_secret():
    payload = b'{"id":"evt_1"}'
    header = _sign(payload, secret="some_other_secret")
    with pytest.raises(SignatureError):
        verify_signature(payload, header, _TEST_SECRET)


def test_verify_signature_rejects_malformed_header():
    payload = b'{"id":"evt_1"}'
    with pytest.raises(SignatureError, match="malformed") as exc_info:
        verify_signature(payload, "garbage,no,delimiters", _TEST_SECRET)
    # Category attached for ops triage — distinguishes "bot probe"
    # from "real Stripe event we mishandled".
    assert exc_info.value.category == "malformed_header"


def test_signature_error_categories_are_distinct():
    """Each failure mode tags the exception with a distinct category
    string so log parsers can route alerts (deploy misconfig vs
    replay attack vs bot probe) without parsing the message."""
    import time

    payload = b'{"id":"evt_1"}'

    with pytest.raises(SignatureError) as e1:
        verify_signature(payload, "t=1,v1=x", "")
    assert e1.value.category == "secret_unset"

    with pytest.raises(SignatureError) as e2:
        verify_signature(payload, "garbage", _TEST_SECRET)
    assert e2.value.category == "malformed_header"

    with pytest.raises(SignatureError) as e3:
        verify_signature(payload, "t=0,v1=x", _TEST_SECRET, now=int(time.time()))
    assert e3.value.category == "stale_timestamp"

    # Real timestamp + wrong signature → mismatch.
    now = int(time.time())
    with pytest.raises(SignatureError) as e4:
        verify_signature(payload, f"t={now},v1=deadbeef", _TEST_SECRET, now=now)
    assert e4.value.category == "signature_mismatch"


def test_verify_signature_rejects_stale_timestamp():
    """Replay older than tolerance window must fail — defends against
    a leaked old payload being replayed by an attacker."""
    payload = b'{"id":"evt_1"}'
    stale_ts = int(time.time()) - SIGNATURE_TOLERANCE_SECONDS - 60
    header = _sign(payload, ts=stale_ts)
    with pytest.raises(SignatureError, match="tolerance"):
        verify_signature(payload, header, _TEST_SECRET)


def test_verify_signature_accepts_secret_rotation():
    """Stripe sends multiple v1= signatures during a webhook secret
    rotation. Any match must be accepted."""
    payload = b'{"id":"evt_1"}'
    ts = int(time.time())
    signed = f"{ts}.".encode() + payload
    sig_old = hmac.new(b"old_secret", signed, hashlib.sha256).hexdigest()
    sig_new = hmac.new(_TEST_SECRET.encode(), signed, hashlib.sha256).hexdigest()
    header = f"t={ts},v1={sig_old},v1={sig_new}"
    verify_signature(payload, header, _TEST_SECRET)  # no raise


def test_verify_signature_rejects_when_secret_empty():
    payload = b'{"id":"evt_1"}'
    header = _sign(payload)
    with pytest.raises(SignatureError, match="not configured"):
        verify_signature(payload, header, "")


# ── Event recording + dedup ─────────────────────────────────────


@pytest.mark.asyncio
async def test_process_event_records_unhandled_type_as_received(
    finmind_session,
):
    """Unknown event_type → still recorded in payment_events for
    audit/replay, just no subscription mutation."""
    event = {
        "id": "evt_test_unknown",
        "type": "ping.something_random",
        "data": {"object": {}},
    }
    outcome = await process_event(finmind_session, event)
    assert outcome.status == "unhandled"

    n = (
        await finmind_session.execute(select(PaymentEvent))
    ).scalars().all()
    assert len(n) == 1
    assert n[0].event_id == "evt_test_unknown"
    assert n[0].processed_at is not None  # marked done (no handler error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    [
        "customer.subscription.trial_will_end",
        "invoice.upcoming",
        "invoice.payment_method_attached",
        "invoice.finalized",
    ],
)
async def test_known_but_unhandled_event_types_round_trip(
    finmind_session, event_type,
):
    """Stripe sends these events in normal subscription lifecycles.
    We don't currently act on them (no notification system wired) but
    the contract is: persist + ack 200 so Stripe stops retrying.
    These tests pin that contract — the day we add a handler, the
    test for that event_type will start failing on `outcome.status`
    and the operator gets a clear signal to update assertions."""
    event = {
        "id": f"evt_{event_type.replace('.', '_')}",
        "type": event_type,
        "data": {"object": {"id": "obj_xyz"}},
    }
    outcome = await process_event(finmind_session, event)

    # Until we wire handlers, these all fall through to the
    # catch-all "unhandled" path.
    assert outcome.status == "unhandled"
    assert outcome.event_type == event_type

    persisted = (
        await finmind_session.execute(
            select(PaymentEvent).where(PaymentEvent.event_type == event_type)
        )
    ).scalars().all()
    assert len(persisted) == 1
    assert persisted[0].processed_at is not None
    assert persisted[0].error is None


@pytest.mark.asyncio
async def test_process_event_dedups_redelivery(finmind_session):
    """Stripe retries failed deliveries up to 3 days. Re-receiving
    the same event_id must NOT double-process."""
    event = {
        "id": "evt_dedup",
        "type": "customer.subscription.created",
        "data": {"object": {
            "id": "sub_dedup_1",
            "metadata": {"owner_email": "x@y.com", "plan_code": "pro"},
            "start_date": int(time.time()),
            "current_period_end": int(time.time()) + 86400,
        }},
    }

    out1 = await process_event(finmind_session, event)
    assert out1.status == "processed"

    out2 = await process_event(finmind_session, event)
    assert out2.status == "duplicate"

    # Still exactly 1 subscription row.
    subs = (await finmind_session.execute(select(Subscription))).scalars().all()
    assert len(subs) == 1


# ── Subscription lifecycle ──────────────────────────────────────


@pytest.mark.asyncio
async def test_subscription_created_inserts_row(finmind_session):
    event = {
        "id": "evt_created_1",
        "type": "customer.subscription.created",
        "data": {"object": {
            "id": "sub_xyz_1",
            "metadata": {"owner_email": "user@example.com", "plan_code": "pro"},
            "start_date": 1714521600,         # 2024-05-01
            "current_period_end": 1717113600, # 2024-05-31
        }},
    }
    outcome = await process_event(finmind_session, event)
    assert outcome.status == "processed"

    sub = (
        await finmind_session.execute(
            select(Subscription).where(Subscription.external_sub_id == "sub_xyz_1")
        )
    ).scalar_one()
    assert sub.owner_email == "user@example.com"
    assert sub.plan_code == "pro"
    assert sub.status == "active"
    assert sub.external_provider == "stripe"


@pytest.mark.asyncio
async def test_subscription_updated_changes_plan_and_status(finmind_session):
    """Created followed by updated → row reflects the new state."""
    create_evt = {
        "id": "evt_upd_create",
        "type": "customer.subscription.created",
        "data": {"object": {
            "id": "sub_upd",
            "metadata": {"owner_email": "u@x.com", "plan_code": "starter"},
            "start_date": int(time.time()),
            "current_period_end": int(time.time()) + 86400,
        }},
    }
    update_evt = {
        "id": "evt_upd_update",
        "type": "customer.subscription.updated",
        "data": {"object": {
            "id": "sub_upd",
            "metadata": {"plan_code": "pro"},
            "status": "past_due",
        }},
    }
    await process_event(finmind_session, create_evt)
    await process_event(finmind_session, update_evt)

    sub = (
        await finmind_session.execute(
            select(Subscription).where(Subscription.external_sub_id == "sub_upd")
        )
    ).scalar_one()
    assert sub.plan_code == "pro"
    assert sub.status == "past_due"


@pytest.mark.asyncio
async def test_subscription_deleted_cancels(finmind_session):
    create_evt = {
        "id": "evt_del_create",
        "type": "customer.subscription.created",
        "data": {"object": {
            "id": "sub_del",
            "metadata": {"owner_email": "u@x.com", "plan_code": "pro"},
            "start_date": int(time.time()),
        }},
    }
    delete_evt = {
        "id": "evt_del",
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_del"}},
    }
    await process_event(finmind_session, create_evt)
    await process_event(finmind_session, delete_evt)

    sub = (
        await finmind_session.execute(
            select(Subscription).where(Subscription.external_sub_id == "sub_del")
        )
    ).scalar_one()
    assert sub.status == "canceled"
    assert sub.expires_at is not None


@pytest.mark.asyncio
async def test_invoice_payment_failed_marks_past_due(finmind_session):
    create_evt = {
        "id": "evt_pf_create",
        "type": "customer.subscription.created",
        "data": {"object": {
            "id": "sub_pf",
            "metadata": {"owner_email": "u@x.com", "plan_code": "pro"},
            "start_date": int(time.time()),
        }},
    }
    fail_evt = {
        "id": "evt_pf",
        "type": "invoice.payment_failed",
        "data": {"object": {"subscription": "sub_pf"}},
    }
    await process_event(finmind_session, create_evt)
    await process_event(finmind_session, fail_evt)

    sub = (
        await finmind_session.execute(
            select(Subscription).where(Subscription.external_sub_id == "sub_pf")
        )
    ).scalar_one()
    assert sub.status == "past_due"


@pytest.mark.asyncio
async def test_subscription_updated_without_prior_created_falls_back(
    finmind_session,
):
    """Stripe sometimes delivers `updated` before we've processed the
    initial `created` (out-of-order delivery / catch-up after our
    endpoint was down). The handler must INSERT rather than 404."""
    update_evt = {
        "id": "evt_oo",
        "type": "customer.subscription.updated",
        "data": {"object": {
            "id": "sub_oo",
            "metadata": {"owner_email": "u@x.com", "plan_code": "pro"},
            "start_date": int(time.time()),
            "current_period_end": int(time.time()) + 86400,
        }},
    }
    outcome = await process_event(finmind_session, update_evt)
    assert outcome.status == "processed"

    sub = (
        await finmind_session.execute(
            select(Subscription).where(Subscription.external_sub_id == "sub_oo")
        )
    ).scalar_one()
    assert sub.owner_email == "u@x.com"


# ── End-to-end via FastAPI ─────────────────────────────────────


@pytest.fixture
def app(finmind_session):
    fastapi_app = FastAPI()
    fastapi_app.include_router(router, prefix="/api/finmind")

    async def _override():
        yield finmind_session

    fastapi_app.dependency_overrides[get_finmind_db] = _override
    return fastapi_app


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_endpoint_503_when_secret_unset(client, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    resp = await client.post(
        "/api/finmind/webhooks/stripe",
        content=b"{}",
        headers={"Stripe-Signature": "t=1,v1=x"},
    )
    assert resp.status_code == 503
    assert "STRIPE_WEBHOOK_SECRET" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_endpoint_401_on_bad_signature(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _TEST_SECRET)
    resp = await client.post(
        "/api/finmind/webhooks/stripe",
        content=b'{"id":"evt_x","type":"ping"}',
        headers={"Stripe-Signature": _sign(b'{"different":"payload"}')},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_endpoint_400_on_missing_signature(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _TEST_SECRET)
    resp = await client.post(
        "/api/finmind/webhooks/stripe",
        content=b"{}",
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_endpoint_200_on_valid_sig_persists_event(
    client, finmind_session, monkeypatch,
):
    """End-to-end happy path — sign payload, POST, expect 200, assert
    payment_events row + subscription state change."""
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _TEST_SECRET)

    body = json.dumps({
        "id": "evt_e2e",
        "type": "customer.subscription.created",
        "data": {"object": {
            "id": "sub_e2e",
            "metadata": {"owner_email": "e2e@x.com", "plan_code": "pro"},
            "start_date": int(time.time()),
            "current_period_end": int(time.time()) + 86400,
        }},
    }).encode()

    resp = await client.post(
        "/api/finmind/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    body_json = resp.json()
    assert body_json["status"] == "processed"
    assert body_json["event_type"] == "customer.subscription.created"

    # Row landed.
    sub = (
        await finmind_session.execute(
            select(Subscription).where(Subscription.external_sub_id == "sub_e2e")
        )
    ).scalar_one()
    assert sub.plan_code == "pro"
