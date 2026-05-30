"""Owner-scoped context blocks for ``gather_market_context``: the
user-context summary (portfolio + watchlist + focus overlap) and the
prior-discussions cross-session memory list.

Extracted from ``services.discussion_service`` as the C3-1 β slice in
``misty-mixing-harbor.md``. Both functions are owner-scoped DB
aggregations called from ``discussion/context/blocks/owner.py`` (which
lazy-imports them via ``discussion_service`` for back-compat) and
exercised directly by ``test_discussion_service.py`` (~10 test cases
that reach in by name).

Design choices preserved verbatim from the original site:

  * ``_assemble_user_context`` — no live-quote enrichment. The
    discussion isn't about today's exact P&L, it's about portfolio
    fit, sector concentration, and overlap with the topic. Each
    list (holdings, watchlist_symbols) is capped at the constants
    below so the prompt budget stays bounded even for power users
    with many portfolios.
  * ``_assemble_prior_discussions`` — owner-scoped, focus-symbol-
    matched cross-session recall. Personas have no implicit memory
    across sessions; this block surfaces the headline of recent
    concluded discussions that overlap the topic so they can stay
    self-consistent. Capped + lookback-clamped to keep the prompt
    bounded.
  * ``as_of`` (PR #224) clamps the prior-discussions lookup window
    to discussions concluded BEFORE that timestamp — backtest mode
    prevents future leakage.

``STATUS_DONE`` is lazy-imported from ``discussion_service`` at call
time (same pattern the synthesizer uses) to keep the module-load
graph acyclic.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion

log = logging.getLogger(__name__)


# ── user_context (owner's portfolio + watchlist) ───────────────────
#
# Read directly off the ORM with no live-quote enrichment: the
# discussion isn't about today's exact P&L, it's about portfolio
# fit, sector concentration, and overlap with the topic. Cap each
# list (holdings, watchlist_symbols) so the prompt budget stays
# bounded even for power users with many portfolios.
#
# Privacy: round_context snapshots persist this block, but
# discussions are owner-scoped both at the API layer and via
# explicit `Holding.portfolio_id`/`Watchlist.user_id` filters —
# nothing leaks across users.

_USER_CONTEXT_HOLDING_CAP = 20
_USER_CONTEXT_WATCHLIST_CAP = 30


async def _assemble_user_context(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    focus_symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Compact summary of the discussion owner's portfolio + watchlist.

    Cheap (no live quote enrichment) — suitable to fire on every
    round. Each sub-block degrades to an empty list on query failure
    so a transient portfolio-table outage doesn't kill the round.
    """
    from models.portfolio import Holding, Portfolio
    from models.watchlist import Watchlist, WatchlistItem

    out: dict[str, Any] = {
        "portfolios":        [],
        "holdings":          [],
        "watchlist_symbols": [],
        "focus_overlap":     {"held": [], "watching": []},
    }
    focus_set = {s for s in (focus_symbols or []) if s}

    try:
        pf_stmt = (
            select(Portfolio)
            .where(Portfolio.user_id == owner_id)
            .order_by(Portfolio.created_at)
        )
        portfolios = list((await db.scalars(pf_stmt)).all())
    except Exception as exc:
        log.warning("user_context.portfolios.failed", extra={"error": str(exc)})
        portfolios = []

    holdings_rows: list[dict[str, Any]] = []
    for p in portfolios:
        try:
            h_stmt = select(Holding).where(Holding.portfolio_id == p.id)
            hs = list((await db.scalars(h_stmt)).all())
        except Exception as exc:
            log.warning("user_context.holdings.failed",
                        extra={"portfolio_id": str(p.id), "error": str(exc)})
            hs = []
        out["portfolios"].append({
            "name":          p.name,
            "currency":      p.currency,
            "holding_count": len(hs),
        })
        for h in hs:
            holdings_rows.append({
                "portfolio":     p.name,
                "symbol":        h.symbol,
                "market":        h.market.value,
                "quantity":      float(h.quantity),
                "avg_cost":      float(h.avg_cost),
                "cost_currency": h.cost_currency,
            })

    # Largest-position-first so the cap prefers the meaningful holdings.
    holdings_rows.sort(
        key=lambda r: float(r["quantity"]) * float(r["avg_cost"]),
        reverse=True,
    )
    out["holdings"] = holdings_rows[:_USER_CONTEXT_HOLDING_CAP]

    try:
        wl_stmt = (
            select(WatchlistItem)
            .join(Watchlist, WatchlistItem.watchlist_id == Watchlist.id)
            .where(Watchlist.user_id == owner_id)
        )
        wl_items = list((await db.scalars(wl_stmt)).all())
    except Exception as exc:
        log.warning("user_context.watchlist.failed", extra={"error": str(exc)})
        wl_items = []

    seen_wl: set[tuple[str, str]] = set()
    watchlist_summary: list[dict[str, str]] = []
    for it in wl_items:
        key = (it.market.value, it.symbol)
        if key in seen_wl:
            continue
        seen_wl.add(key)
        watchlist_summary.append({"symbol": it.symbol, "market": it.market.value})
    out["watchlist_symbols"] = watchlist_summary[:_USER_CONTEXT_WATCHLIST_CAP]

    if focus_set:
        held_syms = {r["symbol"] for r in holdings_rows}
        wl_syms = {it["symbol"] for it in watchlist_summary}
        out["focus_overlap"] = {
            "held":     sorted(focus_set & held_syms),
            "watching": sorted(focus_set & wl_syms),
        }

    return out


# ── prior_discussions (cross-discussion memory) ───────────────────
#
# Personas have no recall across sessions: each new discussion sees
# only the current turn list. To stay self-consistent (and surface
# follow-ups on prior recommendations), we attach a short summary of
# the owner's recent concluded discussions that touch any of the
# current focus_symbols.
#
# Owner-scoped (the FK + WHERE clause both hard-gate to the user);
# the current discussion's own id is excluded so a re-run doesn't
# reference itself.

_PRIOR_DISCUSSIONS_CAP = 5
_PRIOR_DISCUSSIONS_LOOKBACK_DAYS = 90


async def _assemble_prior_discussions(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    focus_symbols: list[str] | None,
    exclude_id: uuid.UUID | None = None,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    """Most-recent-first list of the owner's concluded discussions
    that overlap any of `focus_symbols` (matched against the topic
    string or against `conclusion.recommended_symbols`).

    Capped at `_PRIOR_DISCUSSIONS_CAP` and limited to the last
    `_PRIOR_DISCUSSIONS_LOOKBACK_DAYS` days so a 2-year-old call is
    not dragged into a fresh discussion's prompt.

    `as_of` (PR #224) clamps the lookup window to discussions
    concluded BEFORE that timestamp — backtest mode prevents
    "future leakage" where a 2026-04-22 discussion would otherwise
    surface in a 2026-01-15 backtest's prior list.

    The block is intentionally compact (no full conclusion reasoning,
    no risks list) — personas only need the headline so they can
    stay consistent. They can refer the user back to the prior
    discussion id for full detail.
    """
    if not focus_symbols:
        return []

    # Lazy-import the status sentinel from discussion_service so the
    # module-load graph stays acyclic — same pattern the synthesizer
    # uses for `get_turns` / `gather_market_context` / etc.
    from services.discussion_service import STATUS_DONE

    anchor = as_of or datetime.now(UTC)
    cutoff = anchor - timedelta(days=_PRIOR_DISCUSSIONS_LOOKBACK_DAYS)
    stmt = (
        select(Discussion)
        .where(
            Discussion.owner_id == owner_id,
            Discussion.status == STATUS_DONE,
            Discussion.conclusion.isnot(None),
            Discussion.created_at >= cutoff,
            Discussion.created_at < anchor,
        )
        .order_by(Discussion.created_at.desc())
        .limit(50)  # generous cap for the in-Python filter
    )
    if exclude_id is not None:
        stmt = stmt.where(Discussion.id != exclude_id)
    rows = list((await db.scalars(stmt)).all())
    if not rows:
        return []

    focus_set = {str(s) for s in focus_symbols}
    matches: list[dict[str, Any]] = []
    for row in rows:
        topic = row.topic or ""
        conclusion = row.conclusion if isinstance(row.conclusion, dict) else {}
        recommended = [
            str(s).strip()
            for s in (conclusion.get("recommended_symbols") or [])
            if str(s).strip()
        ]
        # Match either: focus symbol literally appears in the topic
        # string (catches the `2330` / `$AAPL` case the user typed),
        # or appears in the prior conclusion's recommended_symbols.
        matched = sorted({
            sym for sym in focus_set
            if sym in topic or sym in recommended
        })
        if not matched:
            continue
        matches.append({
            "id":                  str(row.id),
            "created_at":          row.created_at.isoformat(),
            # PR #278: surface the historical anchor for backtest
            # discussions so the persona prompt + frontend summary
            # show "the day this prior discussion was analysing",
            # not the day it was created. NULL for live discussions
            # — caller (frontend / `_format_history`) falls back to
            # `created_at` when missing.
            "as_of_date":          (
                row.as_of_date.isoformat() if row.as_of_date else None
            ),
            "topic":               topic[:120],
            "recommended_symbols": recommended[:5],
            "time_horizon":        conclusion.get("time_horizon"),
            "consensus_score":     conclusion.get("consensus_score"),
            "verdict":             row.verdict,
            "matched_symbols":     matched,
        })
        if len(matches) >= _PRIOR_DISCUSSIONS_CAP:
            break
    return matches
