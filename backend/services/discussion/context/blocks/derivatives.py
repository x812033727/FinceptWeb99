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


async def fetch_large_trader_positioning(
    ctx: dict[str, Any],
    *,
    as_of: date | None,
    record_error: ErrorRecorder,
) -> None:
    """Populate `ctx['large_trader_positioning']` with the TX
    large-trader OI concentration + dealer-volume breadth snapshot
    read from the `finmind.*` archive (`tw_derivatives_archive.
    large_trader_positioning`).

    Unlike `fetch_taifex_positioning` above (a live FinMind call with
    its own cache, needing no DB), this reader is archive-only and
    takes a `db` session. The builder's shared `db` is threaded
    sequentially through the chip/risk blocks around this one in
    `builder.py`, and SQLAlchemy `AsyncSession` is not safe to use
    for two overlapping awaits — so rather than depend on exactly
    where in that sequence this call lands (today or after a future
    reshuffle), this block opens its own short-lived
    `AsyncSessionLocal`, the same idiom `news.py`'s
    `fetch_per_symbol_sentiment` uses for its per-symbol fan-out.

    The reader returning `None` (archive has nothing for this cut)
    is a valid no-signal state, not a failure: the key is set to
    `None` and no error is recorded, matching
    `fetch_taifex_positioning`'s empty-state handling above.
    """
    try:
        from db.session import AsyncSessionLocal
        from services.tw_derivatives_archive import large_trader_positioning
        async with AsyncSessionLocal() as db:
            ctx["large_trader_positioning"] = await large_trader_positioning(
                db, as_of=as_of,
            )
    except Exception as exc:
        record_error("large_trader_positioning", exc)
