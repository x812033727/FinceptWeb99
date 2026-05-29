"""Announcements API surface (PR-D4).

Exposes the `corporate_announcements` archive (populated by the
TW MOPS cron in PR-D1 + the US SEC EDGAR cron in PR-D3) for
dashboard / discussion-detail-page consumption.

Single market-parameterized endpoint instead of mounting under
`/api/{tw,us}/...` because the underlying schema is a single table
with a `market` column — the read tier already knows how to scope.
Cleaner than duplicating routes per market.
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from dependencies import get_current_user

router = APIRouter()
CurrentUser = Annotated[dict, Depends(get_current_user)]


@router.get("/recent")
async def announcements_recent(
    _: CurrentUser,
    market: Literal["TW", "US"] = Query(
        ...,
        description=(
            "Which market's announcements to return. TW reads MOPS "
            "重大訊息 (PR-D1); US reads SEC EDGAR 8-K filings "
            "(PR-D3). GLOBAL is intentionally not exposed because "
            "macro discussions don't bind to a single issuer-"
            "disclosure feed."
        ),
    ),
    symbol: str | None = Query(
        None,
        description=(
            "Optional ticker filter — returns only that issuer's "
            "announcements. When omitted, returns the cross-symbol "
            "market view."
        ),
        max_length=20,
    ),
    limit: int = Query(20, ge=1, le=50),
    max_age_days: int = Query(7, ge=1, le=90),
):
    """Recent corporate disclosures from the ingest archive — DB
    only, no live upstream fallback. Returns rows with sentiment
    score / label populated by the hourly scorer (PR-D1b) so the
    dashboard can render coloured badges without an extra
    round-trip.

    Empty list when the archive doesn't have anything for the
    requested window — most commonly hits on a fresh deploy
    before the first cron tick lands rows.
    """
    from services.ingest.repository import (
        read_recent_announcements_autosession,
    )

    try:
        return await read_recent_announcements_autosession(
            market,
            symbol=(symbol.upper() if symbol else None),
            limit=limit,
            max_age_days=max_age_days,
        )
    except Exception as exc:  # noqa: BLE001 — surface as 502 not 500
        raise HTTPException(
            status_code=502,
            detail=f"announcements archive read failed: {exc}",
        )
