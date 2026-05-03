"""Overseas market indicators block (HTTP-bound, yfinance).

TW market opens after the US close — personas reasoning about the
TW open need overnight US direction (SOX / NDX / SPX / VIX) or
they over-weight TW-internal signals and miss fed-policy /
chip-leadership / risk-off shifts that propagate within hours.

Pulled lazily through ``services.overseas_market_service`` (5-min
Redis cache, no new ingest cron). Same fail-soft pattern as the
other context blocks: connector failure leaves the key None and
records into ``ctx['errors']`` so personas can mention "we have
no overseas read" instead of seeing a stale value.

Wired in for ALL markets, not just TW. Fed / SOX direction is
relevant to a US discussion too (just less novel since US
personas would already know overnight direction from the same
data; the block costs nothing to include).
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable

log = logging.getLogger(__name__)

ErrorRecorder = Callable[[str, Exception], None]


async def fetch_overseas_indicators(
    ctx: dict[str, Any],
    *,
    as_of: date | None,
    record_error: ErrorRecorder,
) -> None:
    """Populate ``ctx['overseas_indicators']`` with the curated
    overseas index universe + 1-day % change.

    Failure (yfinance outage / IP-block) leaves the key as None and
    logs into ``ctx['errors']``. Personas treat None as "no
    overseas read available" and weight TW-internal signals as
    they would today.
    """
    try:
        from services.overseas_market_service import get_overseas_snapshot
        result = await get_overseas_snapshot(as_of=as_of)
        ctx["overseas_indicators"] = result
    except Exception as exc:
        record_error("overseas_indicators", exc)
