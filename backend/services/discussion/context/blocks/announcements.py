"""TW MOPS material-info disclosures block (PR-D1).

Reads `corporate_announcements` for the discussion's market and
surfaces two views:

  - `corporate_announcements.market` — top-N most recent disclosures
    across the entire market in the last 7 days, useful for sector-
    wide signals (a wave of 財報 / 庫藏股 disclosures often presages
    a sector move).

  - `corporate_announcements.per_symbol` — per focus_symbol, the
    most recent disclosures for that specific issuer. Personas
    typically value this more than market-wide news because it's
    the issuer's own filing, structured by category, and timestamped
    to the minute.

Both views read the same `read_recent_announcements` helper so the
sentiment fields populate uniformly once the hourly scorer extends
to the announcements archive.

Backtest correctness: `read_recent_announcements` filters by
`announced_at >= now - max_age_days` (live) or anchored to the
discussion's `as_of_dt` (backtest). The latter prevents a sweep
discussion at as_of=2025-06-01 from reading a 2025-06-15 disclosure
— same backtest-safety contract as the news sentiment block.

TW-only: MOPS is the source. US 8-K coverage will land in PR-D3
under a parallel `sec_filings` block; this one stays
market-conditioned so the dispatch is explicit at the call site.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

ErrorRecorder = Callable[[str, Exception], None]

_MAX_AGE_DAYS_LIVE = 7
_MAX_AGE_DAYS_BACKTEST = 14
_DEFAULT_MARKET_LIMIT = 10
_DEFAULT_PER_SYMBOL_LIMIT = 5
_MAX_FOCUS_FANOUT = 5


async def fetch_corporate_announcements(
    ctx: dict[str, Any],
    db: "AsyncSession",
    *,
    market: str,
    focus_symbols: list[str] | None,
    as_of_dt: datetime | None,
    record_error: ErrorRecorder,
) -> None:
    """Populate `ctx["corporate_announcements"]` with the canonical
    `{market: [...], per_symbol: {sym: [...], ...}}` shape.

    Empty list / dict means "no rows in window", not "block failed"
    — the personas read this verbatim. If the underlying read raises
    we record the error and leave the default empty shape so the
    discussion still proceeds.
    """
    if (market or "").upper() != "TW":
        # US/GLOBAL markets don't have a MOPS feed. The default
        # empty shape is set in `_initial_ctx`; we just no-op here
        # so the dispatch is explicit + grep-able.
        return

    try:
        from services.ingest.repository import read_recent_announcements

        max_age_days = (
            _MAX_AGE_DAYS_BACKTEST if as_of_dt is not None
            else _MAX_AGE_DAYS_LIVE
        )

        market_rows = await read_recent_announcements(
            db, market,
            symbol=None, limit=_DEFAULT_MARKET_LIMIT,
            max_age_days=max_age_days,
        )

        per_symbol: dict[str, list[dict[str, Any]]] = {}
        for sym in (focus_symbols or [])[:_MAX_FOCUS_FANOUT]:
            sym_rows = await read_recent_announcements(
                db, market,
                symbol=sym, limit=_DEFAULT_PER_SYMBOL_LIMIT,
                max_age_days=max_age_days,
            )
            if sym_rows:
                per_symbol[sym] = sym_rows

        ctx["corporate_announcements"] = {
            "market": market_rows,
            "per_symbol": per_symbol,
        }
    except Exception as exc:
        record_error("corporate_announcements", exc)
