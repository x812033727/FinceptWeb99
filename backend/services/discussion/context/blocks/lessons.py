"""Owner-scoped past-discussion lessons block.

Reads `discussion_lessons` (PR #learning-mechanism) for the current
owner + market and surfaces them in the ctx so personas see prior
post-mortem takeaways alongside the live signals.

Two buckets are populated:

  - `recent_lessons.market` — top-N market-wide lessons by
    time-decay score (no symbol boost). Useful for sector-rotation
    and macro-style lessons that don't bind to one stock.

  - `recent_lessons.per_symbol` — per focus_symbol, top-N lessons
    biased toward those that mentioned the symbol. The boost lets
    a "外資台指期 1500 口多單訊號被忽略 → 隔日 +5%" lesson surface
    high in TSMC's discussion ctx without crowding out unrelated
    macro lessons.

Master switch: when `LESSONS_INJECTION_ENABLED` is False the block
returns the empty default shape and skips the DB read entirely.

Backtest correctness is enforced by the underlying service:
`fetch_relevant_lessons(..., discussion_as_of=as_of)` filters out
lessons learned after the discussion's anchor date so a sweep
discussion can't peek at its own future.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Any, Callable
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

ErrorRecorder = Callable[[str, Exception], None]


async def fetch_recent_lessons(
    ctx: dict[str, Any],
    db: "AsyncSession",
    *,
    owner_id: UUID,
    market: str,
    focus_symbols: list[str] | None,
    as_of: date | None,
    record_error: ErrorRecorder,
) -> None:
    try:
        from config import settings
        try:
            from services.runtime_config_service import (
                get_bool as _get_bool,
                get_int as _get_int,
            )
            enabled = await _get_bool(db, "LESSONS_INJECTION_ENABLED")
            market_limit = await _get_int(db, "LESSONS_PER_MARKET_LIMIT")
            symbol_limit = await _get_int(db, "LESSONS_PER_SYMBOL_LIMIT")
        except Exception:
            enabled = settings.LESSONS_INJECTION_ENABLED
            market_limit = settings.LESSONS_PER_MARKET_LIMIT
            symbol_limit = settings.LESSONS_PER_SYMBOL_LIMIT

        if not enabled:
            ctx["recent_lessons"] = {"market": [], "per_symbol": {}}
            return

        from services.discussion_lesson_service import (
            fetch_relevant_lessons,
            summary_to_dict,
        )

        market_rows = await fetch_relevant_lessons(
            db,
            owner_user_id=owner_id,
            market=market,
            focus_symbols=set(),
            discussion_as_of=as_of,
            limit=market_limit,
        )

        per_symbol: dict[str, list[dict[str, Any]]] = {}
        if focus_symbols:
            # Cap per-symbol fan-out at 5 so a topic enumerating many
            # codes can't blow the prompt budget. extract_focus_symbols
            # already caps at _MAX_FOCUS_SYMBOLS upstream; this is
            # defensive.
            for sym in list(focus_symbols)[:5]:
                rows = await fetch_relevant_lessons(
                    db,
                    owner_user_id=owner_id,
                    market=market,
                    focus_symbols={sym},
                    discussion_as_of=as_of,
                    limit=symbol_limit,
                )
                if rows:
                    per_symbol[sym] = [summary_to_dict(r) for r in rows]

        ctx["recent_lessons"] = {
            "market": [summary_to_dict(r) for r in market_rows],
            "per_symbol": per_symbol,
        }

        try:
            from middleware.metrics import LESSONS_INJECTED_TOTAL
            if market_rows:
                LESSONS_INJECTED_TOTAL.labels(
                    market=market, scope="market",
                ).inc(len(market_rows))
            for rows in per_symbol.values():
                LESSONS_INJECTED_TOTAL.labels(
                    market=market, scope="per_symbol",
                ).inc(len(rows))
        except Exception:
            pass
    except Exception as exc:
        record_error("recent_lessons", exc)
