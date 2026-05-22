"""Display-name lookup for symbols surfaced in discussion results.

The synthesizer's structured output (``recommended_symbols``) and the
scoreboard's per-row payload carry only bare codes — ``2330`` /
``AAPL`` / ``BTC``. Humans skimming the conclusion card prefer
``台積電 (2330)``; this module resolves the company name when an
in-memory lookup is available and lets callers fall back to the bare
code otherwise.

Markets covered:

- **TW**: ``tw_market_service.get_company_name`` — in-memory
  ``_name_map`` refreshed daily by the symbol-map cron. O(1) lookup.
- **CRYPTO**: static ``data.crypto.symbols.NAMES`` dict for the
  Top-20 universe (``BTC -> "Bitcoin"``).
- **US** / **GLOBAL**: intentionally not handled — yfinance ``longName``
  exists but has no zero-cost cached accessor today; pulling it for
  every conclusion render would add a per-symbol upstream call. Bare
  symbol still surfaces, which is the existing behaviour.

TW codes are also pattern-detected (4-6 digit numeric, optional
``R``/``L`` 反向/槓桿 suffix) so a discussion whose ``market`` field
isn't ``TW`` — legacy rows that defaulted before the field existed,
or a GLOBAL/US discussion that recommended a Taiwan stock — still
picks up names from the TW map. The pattern is disjoint from US
tickers (alphabetic) and crypto codes (alphabetic), so the broader
lookup doesn't risk false positives.

Pure / no DB / no I/O. ``screener_utils._tag_industry`` already wires
the same TW name map into context rows; this helper applies the same
lookup at result-serialization time so the names survive the
structured-conclusion bottleneck.
"""
from __future__ import annotations

import re
from typing import Iterable

# 4-6 digit TW securities code, optionally followed by a single uppercase
# letter (``R`` for 反向 ETF, ``L`` for 槓桿 ETF). Matches the same shape
# ``services.tw_market_service._ETF_CODE`` accepts plus regular stock
# codes like ``2330`` / ``6415`` and 6-digit emerging ones like ``911616``.
_TW_CODE_PATTERN = re.compile(r"^\d{4,6}[A-Z]?$")


def _looks_like_tw_code(symbol: str) -> bool:
    return bool(_TW_CODE_PATTERN.match(symbol))


def resolve_display_name(market: str, symbol: str) -> str | None:
    m = (market or "").upper()
    if not symbol:
        return None
    # Pattern-detect TW codes regardless of ``market`` so legacy rows
    # whose market wasn't normalised to ``TW`` still surface 公司簡稱
    # in the chip. ``_name_map`` is per-process: a miss falls through
    # to the market-keyed branches below.
    if _looks_like_tw_code(symbol):
        from services import tw_market_service
        name = tw_market_service.get_company_name(symbol)
        if name:
            return name
    if m == "TW":
        from services import tw_market_service
        return tw_market_service.get_company_name(symbol)
    if m == "CRYPTO":
        from data.crypto.symbols import NAMES
        return NAMES.get(symbol.upper())
    return None


def build_symbol_names_map(
    market: str, symbols: Iterable[str] | None,
) -> dict[str, str]:
    out: dict[str, str] = {}
    for s in symbols or []:
        if not s or s in out:
            continue
        name = resolve_display_name(market, s)
        if name:
            out[s] = name
    return out


def enrich_conclusion_with_names(
    market: str, conclusion: dict | None,
) -> dict | None:
    """Add a ``symbol_names`` mapping to a conclusion dict in place
    and return it. Covers both ``recommended_symbols`` and the
    per-pick ``recommendations[].symbol`` list because the two are
    overlapping-but-not-identical (older conclusions only carry the
    flat array; PR-C0+ also emit ``recommendations``).

    Returns ``conclusion`` unchanged when it's None / not a dict so
    callers can apply it unconditionally to optional fields like
    ``post_mortem_conclusion``.
    """
    if not isinstance(conclusion, dict):
        return conclusion
    symbols: list[str] = []
    for s in conclusion.get("recommended_symbols") or []:
        if isinstance(s, str):
            symbols.append(s)
    for rec in conclusion.get("recommendations") or []:
        if isinstance(rec, dict):
            sym = rec.get("symbol")
            if isinstance(sym, str):
                symbols.append(sym)
    conclusion["symbol_names"] = build_symbol_names_map(market, symbols)
    return conclusion
