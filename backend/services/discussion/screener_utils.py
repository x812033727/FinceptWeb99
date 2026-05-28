"""Screener row compaction + industry tagging for the discussion ctx.

Three shapes:

  - `_compact_screener_row(r)` — TW screener row → minimal dict the
    persona prompt actually needs. Drops the 12 fields the LLM
    doesn't read; pulls industry / company-name from the in-memory
    `tw_market_service` maps so per-row enrichment is O(1).
  - `_compact_us_screener_row(r)` — US-side parallel. Industry +
    sector come from the row itself (Polygon snapshot tier carries
    them); PE / yield omitted because the snapshot tier doesn't
    populate them reliably.
  - `_tag_industry(rows)` — pass-through enricher. For aggregator
    outputs already shaped (top foreign buyers, revenue growers,
    active buybacks, single-stock futures OI) — adds `industry`
    and `name_zh` if missing, leaves pre-tagged rows alone.

Plus `_is_speculative_etf(sym)` — TW leveraged-ETF guard
(`<code>L`/`U`/`R` suffixes signal 2x bull / bear / inverse, which
free-tier short-term personas should treat as off-limits regardless
of their PE/PB filter; the regex is anchored against the trailing
suffix to avoid false positives on regular 5-digit OTC codes).

Lifted out of the monolithic ``discussion_service`` so the new
``services/discussion/context/blocks/chip.py`` and ``http.py`` can
import these directly instead of via the ``from
services.discussion_service import ...`` lazy-import dance that
existed only because the helpers used to live in the monolith.
The symbols are re-exported from ``discussion_service`` for any
remaining call sites that haven't migrated yet.
"""
from __future__ import annotations

import re
from typing import Any

# TW leveraged-ETF guard. The free-tier "short-term + value" personas
# fail on these because their amplification masks fundamentals
# (a 0050 yield filter that includes 00631L will show "yield 12%",
# which is the leveraged version's IRR, not a yield). 5-digit OTC
# codes (e.g. 92xxx) are deliberately allowed — they don't share the
# trailing-digit-only shape and pass the filter.
_TW_SPECULATIVE_ETF_RE = re.compile(r"^\d{4,5}[LUR]$")


def _is_speculative_etf(symbol: Any) -> bool:
    if not isinstance(symbol, str):
        return False
    return bool(_TW_SPECULATIVE_ETF_RE.match(symbol))


def _compact_screener_row(
    r: dict[str, Any],
    *,
    as_of_session: str | None = None,
    is_intraday: bool = False,
) -> dict[str, Any]:
    """Strip the screener row to just the fields a persona needs, so the
    LLM prompt stays compact (300 rows × 12 fields fills the context fast).

    `as_of_session` / `is_intraday` stamp each row with the trading
    session its price came from. TW live mode pulls from
    `STOCK_DAY_ALL` which only refreshes post-14:30 Taipei → during
    intraday the price is yesterday's close, and we want personas to
    see that explicitly rather than infer "today" from `captured_at`.
    """
    from services import tw_market_service
    sym = r.get("symbol")
    return {
        "symbol": sym,
        "name": r.get("name_zh") or r.get("name") or (
            tw_market_service.get_company_name(sym) if sym else None
        ),
        "industry": tw_market_service.get_industry(sym) if sym else None,
        "price": r.get("price"),
        "change_pct": r.get("change_pct"),
        "volume": r.get("volume"),
        "pe": r.get("pe_ratio"),
        "yield": r.get("dividend_yield"),
        "as_of_session": as_of_session,
        "is_intraday": is_intraday,
    }


def _compact_us_screener_row(
    r: dict[str, Any],
    *,
    as_of_session: str | None = None,
    is_intraday: bool = False,
) -> dict[str, Any]:
    """US-side compact form (PR #215). Mirrors `_compact_screener_row`
    but pulls from US screener output: industry / sector come from
    the row directly (no global map like TW's `_industry_map`); PE /
    yield often missing on Polygon snapshot tier so they're omitted
    rather than passed through as 0."""
    sym = r.get("symbol")
    return {
        "symbol":     sym,
        "name":       r.get("name"),
        "sector":     r.get("sector"),
        "price":      r.get("price"),
        "change_pct": r.get("change_pct"),
        "volume":     r.get("volume"),
        "as_of_session": as_of_session,
        "is_intraday": is_intraday,
    }


def _tag_industry(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enrich each row with `industry` + `name_zh` from the in-memory
    company-info maps. Rows already carrying these keys are passed
    through unchanged so callers that pre-tagged don't get clobbered.

    Used for the chip-metric and revenue-grower aggregator outputs
    so personas can see "外資買超 2330 (半導體業)" instead of just
    "2330" — the industry tag turns a raw list of codes into
    sector-flow analysis without an extra LLM tool call.
    """
    from services import tw_market_service
    out: list[dict[str, Any]] = []
    for r in rows:
        sym = r.get("symbol")
        enriched = dict(r)
        if sym:
            if "industry" not in enriched or not enriched["industry"]:
                ind = tw_market_service.get_industry(sym)
                if ind:
                    enriched["industry"] = ind
            if "name_zh" not in enriched or not enriched["name_zh"]:
                nm = tw_market_service.get_company_name(sym)
                if nm:
                    enriched["name_zh"] = nm
        out.append(enriched)
    return out
