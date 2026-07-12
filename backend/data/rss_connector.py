"""Generic RSS 2.0 / Atom feed fetcher + parser.

Powers the direct-publisher news feeds registered in
`services.news_sources`. Unlike `data/tw/google_news_tw_connector.py`
(which is bespoke to Google News' title-suffix / redirect-link quirks),
this parses a plain publisher feed: `<item>` (RSS) or `<entry>` (Atom)
with title / link / description / pubDate.

Returns dicts in the SAME shape the ingest task's `_to_row` expects
(`title`, `link`, `source_name`, `description`, `published_at`), minus
`symbol` — symbol tagging is done by the task via the company-name
dictionary, not here.

Design notes:
  - A real browser-ish User-Agent: several TW publishers 403 the
    default httpx UA.
  - `follow_redirects=True` — feedburner / udn bounce through 30x.
  - Namespaced fields (`dc:date`, `content:encoded`) are matched by
    local-name so we don't hard-code namespace URIs.
  - Parse failures return `[]` (logged) rather than raising, EXCEPT
    transport errors (raise_for_status) which propagate so the task's
    backoff arms on a dead feed.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET  # Element type + iteration only
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
# Untrusted external feeds — parse with defusedxml to block XXE /
# billion-laughs / DTD-retrieval attacks that stdlib ET is vulnerable
# to. Only the parse entry point needs to be safe; the returned tree is
# a plain ElementTree.Element.
from defusedxml.ElementTree import fromstring as _safe_fromstring
from defusedxml.common import DefusedXmlException

log = logging.getLogger(__name__)

# Publisher article/feed servers are far pickier than Google News —
# a real UA avoids the 403 wall.
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36 FinceptNewsBot/1.0"
)


def _localname(tag: str) -> str:
    """Strip the `{namespace}` prefix ET prepends to namespaced tags."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_text(el: ET.Element, *names: str) -> str:
    """First non-empty child text matching any of `names` by local-name.
    Handles both RSS (`link` text) and Atom (`link` href attribute)."""
    for child in el:
        ln = _localname(child.tag)
        if ln in names:
            if child.text and child.text.strip():
                return child.text.strip()
            # Atom <link href="..."/> carries the URL in an attribute.
            href = child.get("href")
            if href:
                return href.strip()
    return ""


def _parse_date(raw: str) -> str | None:
    """RFC 822 (RSS pubDate) or ISO 8601 (Atom/dc:date) → ISO UTC.
    Returns None when unparseable so the caller can drop the item."""
    raw = (raw or "").strip()
    if not raw:
        return None
    # Try RFC 822 first (most RSS 2.0 feeds).
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).isoformat()
    except (TypeError, ValueError):
        pass
    # Fall back to ISO 8601 (Atom <published>, dc:date). Accept a
    # trailing Z.
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()
    except ValueError:
        return None


def _parse_items(xml_text: str, limit: int) -> list[dict[str, Any]]:
    try:
        root = _safe_fromstring(xml_text)
    except (ET.ParseError, DefusedXmlException) as exc:
        log.warning("rss.parse_failed", extra={"error": str(exc),
                                                "head": xml_text[:200]})
        return []

    out: list[dict[str, Any]] = []
    # RSS <item> and Atom <entry> both live under the root; match by
    # local-name so a default Atom namespace doesn't hide them.
    for el in root.iter():
        if _localname(el.tag) not in ("item", "entry"):
            continue
        title = _find_text(el, "title")
        link = _find_text(el, "link")
        if not title or not link:
            continue
        published = _parse_date(
            _find_text(el, "pubDate", "date", "published", "updated")
        )
        if published is None:
            continue
        description = _find_text(el, "description", "summary", "encoded")
        out.append({
            "title": title,
            "link": link,
            "description": description,
            "published_at": published,
        })
        if len(out) >= limit:
            break
    return out


async def fetch_feed(url: str, *, limit: int = 60) -> list[dict[str, Any]]:
    """Fetch + parse one RSS/Atom feed. Transport errors propagate
    (task backoff); parse errors yield `[]`."""
    async with httpx.AsyncClient(
        timeout=15.0, follow_redirects=True,
        headers={"User-Agent": _UA},
    ) as c:
        r = await c.get(url)
        r.raise_for_status()
        xml_text = r.text
    return _parse_items(xml_text, limit)
