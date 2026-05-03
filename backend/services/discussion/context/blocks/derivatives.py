"""TW derivatives context blocks (HTTP-bound, FinMind).

Today: TAIFEX 三大法人台指期未平倉 — market-wide directional signal
from the dominant smart-money cohort. Pulled live from FinMind
(quota-managed, 4-hour Redis cache via `derivatives_service`) so
no new ingest cron / table is required.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable

log = logging.getLogger(__name__)

ErrorRecorder = Callable[[str, Exception], None]


async def fetch_taifex_positioning(
    ctx: dict[str, Any],
    *,
    as_of: date | None,
    record_error: ErrorRecorder,
    contract: str = "TX",
) -> None:
    """Populate `ctx['taifex_positioning']` with the latest TX
    contract net-OI snapshot + 5-day change per investor group.

    Failure (FinMind quota / paywall / parse-empty) leaves the key
    as None and logs into `ctx['errors']`. Personas treat None as
    "no derivatives signal available", same as the news / chip
    blocks' empty-state handling.
    """
    try:
        from services.derivatives_service import get_taifex_positioning
        result = await get_taifex_positioning(
            contract=contract, as_of=as_of,
        )
        ctx["taifex_positioning"] = result
    except Exception as exc:
        record_error("taifex_positioning", exc)
