"""TW risk-warning context block (DB-bound).

Three independent reads bundled into one `risk_warnings` dict:
  - `active_dispositions` (處置股)
  - `recent_suspensions` (停止交易)
  - `high_day_trading_ratio` (當沖比過高)

Personas use these as negative filters: don't recommend a 處置股 even
if screening criteria say buy. Each sub-read is wrapped individually
so one ingest hiccup doesn't blank the other two.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

ErrorRecorder = Callable[[str, Exception], None]


async def fetch_risk_warnings(
    ctx: dict[str, Any],
    db: "AsyncSession",
    *,
    as_of: date | None,
    record_error: ErrorRecorder,
) -> None:
    try:
        from services.ingest.repository import (
            read_active_dispositions,
            read_high_day_trading_ratio,
            read_recent_suspensions,
        )
        ctx["risk_warnings"] = {
            "active_dispositions": await read_active_dispositions(
                db, market="TW", limit=20, as_of=as_of,
            ),
            "recent_suspensions": await read_recent_suspensions(
                db, market="TW", days=7, limit=10, as_of=as_of,
            ),
            "high_day_trading_ratio": await read_high_day_trading_ratio(
                db, market="TW", days=1, threshold=0.6, limit=20,
                as_of=as_of,
            ),
        }
    except Exception as exc:
        record_error("risk_warnings", exc)
