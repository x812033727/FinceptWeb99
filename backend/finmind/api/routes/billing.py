"""Stripe webhook receiver for the FinMind clone.

One endpoint mounted under `/webhooks/stripe`. Verifies the
`Stripe-Signature` header (HMAC-SHA256 ±5min tolerance), dedups via
UNIQUE (provider, event_id), and dispatches subscription.{created,
updated,deleted} + invoice.payment_failed via
`finmind.billing.stripe_webhook.process_event`.

Body is read as raw bytes for signature verification, then parsed as
JSON. Reading via `request.body()` rather than the JSON body parser is
critical: any reformatting (e.g. Pydantic re-serialization) breaks the
HMAC.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from finmind.billing.stripe_webhook import (
    SignatureError,
    process_event,
    verify_signature,
)
from finmind.db.session import get_finmind_db

log = logging.getLogger("finmind.api")

router = APIRouter()


@router.post(
    "/webhooks/stripe",
    summary="Stripe webhook receiver — verifies signature + dispatches",
    status_code=status.HTTP_200_OK,
)
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    db: AsyncSession = Depends(get_finmind_db),
) -> dict[str, Any]:
    """One endpoint for all Stripe events. Returns 200 on success
    (Stripe stops retrying), 401 on signature failure (Stripe retries
    until it gives up), 500 on handler crash (Stripe retries up to 3
    days — gives ops time to fix the bug).
    """
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "stripe webhook disabled — set STRIPE_WEBHOOK_SECRET "
                "to enable"
            ),
        )

    if not stripe_signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing Stripe-Signature header",
        )

    body = await request.body()
    try:
        verify_signature(body, stripe_signature, secret)
    except SignatureError as exc:
        # Don't leak which check failed in the HTTP response — same
        # 401 for malformed / stale / mismatched / missing-secret.
        # Avoids the endpoint becoming an oracle for attackers probing
        # the secret. But DO surface the category in the structured
        # log so ops can distinguish "deploy misconfig" from "replay
        # attack" from "bot probe" without scrolling timestamps.
        log.warning(
            "stripe webhook signature rejected: category=%s detail=%s",
            getattr(exc, "category", "unknown"),
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid Stripe signature",
        )

    try:
        event = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"malformed JSON: {exc}",
        )

    outcome = await process_event(db, event)
    return {
        "event_id": outcome.event_id,
        "event_type": outcome.event_type,
        "status": outcome.status,
        "note": outcome.note,
    }
