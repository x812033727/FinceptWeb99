"""Google News RSS — TW edition.

Replaces FinMind's `TaiwanStockNews` (paid-only dataset) as the primary
source for the hourly `ingest_news_tw` cron. Free, no token, no quota
cap. Aggregates cnyes / 經濟日報 / 工商時報 / Yahoo TW / 鉅亨網 etc. so
we don't have to maintain three separate scrapers.

Single-call shape:

    items = await get_news(query="台股 OR 台灣股市", limit=100)

Returned dicts mirror what `tasks/ingest_news_tw.py::_to_row` expects
from the previous FinMind shape — same key set, same semantics — so
the downstream `_to_row` mapper is unchanged.

Symbol tagging:
    Google News doesn't tag articles by stock code. We regex the first
    4-6 digit number in the title and treat it as a TW stock code. If
    no code appears the article is `symbol=None` (market-wide). The
    discussion subsystem's `extract_focus_symbols` uses the same
    pattern, so a discussion mentioning 2330 will pick up titled
    coverage like "台積電(2330)財報".
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

_RSS_URL = "https://news.google.com/rss/search"
_HL = "zh-TW"
_GL = "TW"
_CEID = "TW:zh-Hant"

# Default market-wide query. `OR` is a Google News operator (case-
# sensitive) so this casts a wide net across the most common Chinese
# headline phrasings for "Taiwan stock market". Tuning this is a
# code change rather than a runtime knob — the ingest pipeline is
# downstream of it and changes here ripple into sentiment scoring.
DEFAULT_QUERY = "台股 OR 台灣股市 OR 加權指數"

# A 4-6 digit standalone token. Matches "2330", "00878" (ETF), "6446".
# Anchored on word boundaries so "20240115" in a date doesn't masquerade
# as a stock code.
_TW_SYMBOL_RE = re.compile(r"\b(\d{4,6})\b")

# Google News titles end with " - 出處" (e.g. "台積電大漲 - 經濟日報").
# Splitting this lets us populate `source_name` from the embedded suffix
# when the `<source>` element is missing (some feed entries omit it).
_TITLE_SUFFIX_RE = re.compile(r"\s+-\s+([^-]+)\s*$")


def _extract_symbol(title: str) -> str | None:
    """Return the first 4-6 digit code in `title`, or None.

    First match wins because TW headlines usually lead with the
    primary subject ("**2330** 台積電法說會優於預期" → 2330) and only
    incidentally mention secondary codes near the end.
    """
    m = _TW_SYMBOL_RE.search(title or "")
    return m.group(1) if m else None


def _split_title_publisher(title: str) -> tuple[str, str | None]:
    """Strip a trailing ` - 出處` suffix from the title and return it
    as the publisher name. Returns `(title, None)` when there's no
    recognisable suffix."""
    m = _TITLE_SUFFIX_RE.search(title or "")
    if m is None:
        return title, None
    publisher = m.group(1).strip()
    cleaned = title[: m.start()].strip()
    return (cleaned or title), (publisher or None)


def _parse_pub_date(raw: str) -> str:
    """RFC 822 → ISO 8601 (UTC). Returns the original string if parsing
    fails so the downstream `_to_row` can apply its own fallback rather
    than silently dropping the article."""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return raw
    if dt is None:
        return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


async def get_news(
    *,
    query: str = DEFAULT_QUERY,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch TW market news from Google News RSS.

    Caller is expected to handle exceptions — the ingest task catches
    them, formats via `_format_news_error`, and arms backoff. We let
    `httpx.HTTPStatusError` propagate so the caller can read the status
    code; everything else falls through as a generic transport / parse
    error.
    """
    params = {
        "q":    query,
        "hl":   _HL,
        "gl":   _GL,
        "ceid": _CEID,
    }
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
        r = await c.get(_RSS_URL, params=params)
        r.raise_for_status()
        xml_text = r.text

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning(
            "google_news_tw.parse_failed",
            extra={"error": str(exc), "head": xml_text[:200]},
        )
        return []

    items: list[dict[str, Any]] = []
    for el in root.findall(".//item")[:limit]:
        raw_title = (el.findtext("title") or "").strip()
        link = (el.findtext("link") or "").strip()
        if not raw_title or not link:
            continue

        title, publisher_from_title = _split_title_publisher(raw_title)
        source_el = el.find("source")
        source_text = source_el.text.strip() if (
            source_el is not None and source_el.text
        ) else ""
        publisher = source_text or publisher_from_title

        published_iso = _parse_pub_date(el.findtext("pubDate") or "")
        symbol = _extract_symbol(title)

        items.append({
            "title":        title,
            "link":         link,
            "source_name":  publisher or "",
            "description":  (el.findtext("description") or "").strip(),
            "published_at": published_iso,
            "symbol":       symbol,
        })
    return items
