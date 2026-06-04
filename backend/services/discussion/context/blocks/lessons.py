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
    topic: str | None = None,
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
            _classify_regime,
            fetch_relevant_lessons,
            summary_to_dict,
        )

        # PR-B1: classify the current ctx's regime so fetch can boost
        # lessons learned in the same market state. Builder runs this
        # block after `fetch_index` + `fetch_taiwan_vix` so the inputs
        # are already populated. NULL when ctx lacks signal — the
        # service treats that as neutral (no boost, no penalty).
        current_regime = _classify_regime(ctx)

        # PR-J2: query embedding for semantic-similarity ranking.
        # Built from topic + market + focus_symbols, mirroring the
        # `build_embed_text` format used at lesson-write time so
        # cosine match is well-defined across both sides. Fail-soft
        # — embedding failure (no API key, provider down) returns
        # None and the fetch falls back to the legacy time/symbol/
        # regime-only ranking. We compute it ONCE per round, not
        # per fetch call, so the per-symbol fan-out below shares the
        # same vector.
        query_embedding = await _build_query_embedding(
            db, topic=topic, market=market, focus_symbols=focus_symbols,
        )

        market_rows = await fetch_relevant_lessons(
            db,
            owner_user_id=owner_id,
            market=market,
            focus_symbols=set(),
            discussion_as_of=as_of,
            limit=market_limit,
            current_regime=current_regime,
            query_embedding=query_embedding,
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
                    current_regime=current_regime,
                    query_embedding=query_embedding,
                )
                if rows:
                    per_symbol[sym] = [summary_to_dict(r) for r in rows]

        # Only `market` / `per_symbol` are emitted. The persona prompt
        # schema (`prompts.py` recent_lessons) and the synthesizer read
        # these two keys. The earlier PR-B3 win/miss split keys
        # (`market_misses` / `market_wins` / `per_symbol_misses` /
        # `per_symbol_wins`) were never referenced by any prompt or
        # reader (verified repo-wide, 2026-06) — they only doubled the
        # serialized lessons payload, which is re-sent on every tool
        # iteration. Dropped to cut per-call token size with no
        # behaviour change.
        ctx["recent_lessons"] = {
            "market": [summary_to_dict(r) for r in market_rows],
            "per_symbol": per_symbol,
        }

        # PR-B2: bump usage telemetry for every lesson that actually
        # made it into the prompt. Uses the same lesson_ids we just
        # passed to summary_to_dict — record_lesson_usage is best-
        # effort and won't disturb the ctx assembly on failure.
        used_ids: list[int] = [r.id for r in market_rows]
        for sym in focus_symbols or []:
            sym_rows = per_symbol.get(sym) or []
            used_ids.extend(
                int(e.get("id"))
                for e in sym_rows
                if isinstance(e, dict) and e.get("id") is not None
            )
        if used_ids:
            try:
                from services.lesson_tier_service import (
                    record_lesson_usage,
                )
                await record_lesson_usage(db, lesson_ids=used_ids)
            except Exception as exc:
                log.debug(
                    "recent_lessons.usage_record_failed",
                    extra={"error": str(exc)},
                )

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


async def _build_query_embedding(
    db: "AsyncSession",
    *,
    topic: str | None,
    market: str,
    focus_symbols: list[str] | None,
) -> list[float] | None:
    """PR-J2: produce the per-round query embedding once for both the
    market-wide and per-symbol fetches. Mirrors `lesson_embedding_
    service.build_embed_text` so the query vector lands in the same
    space the lesson vectors do.

    Returns None on any failure path:
      - empty topic + no focus_symbols (nothing to embed against)
      - LESSON_EMBEDDING_ENABLED=False (master switch off)
      - no API key configured (env empty + no system DB row)
      - provider error / malformed response
    The fetch path treats None as "no semantic boost, fall back to
    legacy ranking" so degradation is graceful — never blocks the
    round.
    """
    topic = (topic or "").strip()
    syms = [s for s in (focus_symbols or []) if s]
    if not topic and not syms:
        return None
    try:
        from services.lesson_embedding_service import (
            build_embed_text,
            embed_texts,
        )
        text = build_embed_text(
            topic or "(no-topic-supplied)",
            market=market,
            related_symbols=syms,
        )
        if not text:
            return None
        vectors, _model = await embed_texts(db, [text])
        if not vectors:
            return None
        return vectors[0]
    except Exception as exc:
        log.debug(
            "recent_lessons.query_embed_failed", extra={"error": str(exc)},
        )
        return None
