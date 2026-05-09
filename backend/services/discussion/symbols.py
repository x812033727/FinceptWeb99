"""Symbol extraction from free-text discussion topics.

Pulls stock / crypto codes out of user-supplied topic strings so the
context-builder can fan out per-symbol news / focus briefs / prior-
discussion lookups for the right tickers. Three market-shape rules:

  - **TW**: 4-6 digit numeric codes. Year-like 4-digit values
    (1900-2099) are filtered out so "2026 Q1 法說" doesn't pollute
    the per-symbol sentiment lookup with a date.
  - **US**: cashtag ``$AAPL`` always honoured; bare uppercase 1-5
    letter tokens honoured if they aren't in
    ``_US_TICKER_STOPWORDS`` (common English words like "AND" /
    "FOR" / "USD").
  - **GLOBAL**: cashtags + crypto base assets from the curated Top-20
    universe (BTC / ETH / SOL …). Year-filtered TW codes are also
    honoured for cross-listed TW ADRs (TSM / UMC) that surface in
    international topics.

Plus a TW / GLOBAL name-based fallback (PR #221): topics written
with the company short name ("討論 台積電 / 鴻海 短線走勢") miss the
digit-only regex; the in-memory ``_name_map`` populated by the daily
``tw_market_service`` symbol-refresh cron picks those up so prior
discussions / news / briefs actually find them.

Pure (no DB, no LLM, no I/O — the name-map fallback is one
in-memory dict lookup). ``discussion_service`` re-exports the public
``extract_focus_symbols`` symbol for back-compat with the
``synthesize_conclusion`` + ``gather_market_context`` call sites
that still live in the monolith.
"""
from __future__ import annotations

import re

# Symbol-extraction patterns. Each market uses a different shape (see
# the module docstring for the rationale).
_TW_SYMBOL_RE = re.compile(r"(?<![\w])(\d{4,6})(?![\w])")
_CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})\b")
_BARE_US_TICKER_RE = re.compile(r"\b([A-Z]{1,5})\b")

# Year-like 4-digit numbers — keep generous; TW codes never overlap.
_YEAR_MIN = 1900
_YEAR_MAX = 2099

# Common 1-5 letter uppercase tokens that look like US tickers but
# aren't. Prevents `discussion_service` from sentimening "USD news".
# Not exhaustive — the topic field is short, false positives are
# cheap (worst case: an empty per-symbol news block), and adding new
# entries here is a 1-line patch.
_US_TICKER_STOPWORDS = frozenset({
    "A", "AI", "AN", "ARE", "AS", "AT", "BE", "BY", "CAN", "CEO",
    "CFO", "CTO", "DCF", "DXY", "EPS", "ETF", "EU", "FED", "FOMC",
    "FOR", "FX", "GDP", "GET", "I", "IF", "IN", "IPO", "IS", "IT",
    "M2", "NEW", "NO", "NOT", "OF", "ON", "OR", "PE", "ROE",
    "SEC", "SP", "SPX", "TBD", "THE", "TO", "UK", "US", "USA", "USD",
    "VAR", "VIX", "WTI", "YOU",
})

_DEFAULT_MARKET = "TW"
_MAX_FOCUS_SYMBOLS = 5


def _is_year_like(code: str) -> bool:
    """4-digit numeric tokens in the year range — `2026 Q1 法說` would
    otherwise be tagged as a TW stock code and pollute the per-symbol
    sentiment lookup."""
    if len(code) != 4 or not code.isdigit():
        return False
    return _YEAR_MIN <= int(code) <= _YEAR_MAX


def _crypto_universe() -> list[str]:
    """Top-20 crypto base assets, normalised to uppercase. Imported
    lazily so a unit test that monkeypatches `data.crypto.symbols`
    sees the patched value, and so the discussion service stays
    decoupled from the crypto module loading at import time."""
    try:
        from data.crypto.symbols import TOP20
    except Exception:
        return []
    return [str(s).upper() for s in TOP20 if s]


def extract_focus_symbols(text: str, *, market: str = _DEFAULT_MARKET) -> list[str]:
    """Pull stock / crypto codes out of free text. Deduped, capped at
    `_MAX_FOCUS_SYMBOLS`, returned in encounter order.

    Behaviour by market:
      - TW: 4-6 digit numeric codes; 4-digit year-like values
        (1900-2099) are filtered to avoid mis-tagging dates.
      - US: cashtag `$AAPL` always honoured; bare uppercase 1-5 letter
        tokens honoured if they aren't in `_US_TICKER_STOPWORDS`.
      - GLOBAL: cashtags + crypto base assets from the curated Top-20
        universe (BTC / ETH / SOL …). Year-filtered TW codes are also
        honoured because the international news bucket sometimes
        carries cross-listed TW ADRs (TSM / UMC).

    Cashtag matches are honoured for every market — `$AAPL` in a TW
    discussion still pulls AAPL into the per-symbol news bucket,
    because the user's intent is explicit.
    """
    raw = text or ""
    seen: list[str] = []

    def _push(code: str) -> bool:
        if code not in seen:
            seen.append(code)
        return len(seen) >= _MAX_FOCUS_SYMBOLS

    for tag in _CASHTAG_RE.findall(raw):
        if _push(tag):
            return seen

    market = (market or _DEFAULT_MARKET).upper()
    if market == "TW":
        for code in _TW_SYMBOL_RE.findall(raw):
            if _is_year_like(code):
                continue
            if _push(code):
                break
    elif market == "US":
        for tok in _BARE_US_TICKER_RE.findall(raw):
            if tok in _US_TICKER_STOPWORDS:
                continue
            if _push(tok):
                break
    else:  # GLOBAL — accept TW digits + crypto base assets
        universe = set(_crypto_universe())
        for tok in _BARE_US_TICKER_RE.findall(raw):
            if tok not in universe:
                continue
            if _push(tok):
                return seen
        for code in _TW_SYMBOL_RE.findall(raw):
            if _is_year_like(code):
                continue
            if _push(code):
                break

    # Name-based fallback for TW + GLOBAL markets (PR #221). Topics
    # written with the company short name ("討論台積電 / 鴻海 短線
    # 走勢") miss the digit-only regex. Lookup against the in-memory
    # `_name_map` populated by the daily symbol-refresh cron picks
    # those up so `prior_discussions` / `per_symbol_news_sentiment` /
    # `focus_briefs` actually find them. Skipped for US — different
    # name conventions, and the bare-ticker regex already covers
    # the common case there.
    if market in ("TW", "GLOBAL") and len(seen) < _MAX_FOCUS_SYMBOLS:
        try:
            from services.tw_market_service import (
                find_symbols_by_names_in_text,
            )
            remaining = _MAX_FOCUS_SYMBOLS - len(seen)
            for sym in find_symbols_by_names_in_text(raw, limit=remaining):
                if _push(sym):
                    break
        except Exception:
            # Fresh deploy where symbol map hasn't loaded, or any
            # other defensive failure — fall through with whatever
            # the regex-only pass found.
            pass
    return seen
