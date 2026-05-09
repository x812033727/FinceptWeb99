"""SEC EDGAR — recent 8-K filings connector (PR-D3).

Mirrors `data/tw/mops_announcements_connector` for the US market.
Pulls the most recent Form 8-K filings from SEC EDGAR's "current
filings" ATOM feed — free, public, no auth, no quota beyond the
SEC's documented courtesy limit (10 req/sec; we make ~1 call/hour).

Why 8-K: it's the SEC's "material event" form. Issuers must file
within 4 business days of any of the categorised triggering events
(earnings, M&A, executive change, bankruptcy, etc.). This is the
US analogue of TW MOPS 重大訊息.

Endpoint:
    https://www.sec.gov/cgi-bin/browse-edgar
        ?action=getcurrent&type=8-K&output=atom&count=100

Returns up to `count` most recent filings across all issuers as
ATOM XML. Each entry has:
  - <title>     "8-K - COMPANY NAME (0000123456) (Filer)"
  - <link>      Filing index page on edgar.sec.gov
  - <updated>   ISO 8601 timestamp (always UTC)
  - <summary>   Filing-type human description (often empty for 8-K)
  - <id>        Unique URN

Symbol resolution: SEC indexes by CIK (Central Index Key, a 10-digit
zero-padded integer); we map CIK → ticker via the public
`company_tickers.json` map. Issuers without a US-listed ticker
(foreign filers, private SPVs) are silently dropped at the
normalisation step — those rows wouldn't be useful to the discussion
ctx since personas reason in tickers.

SEC compliance: the documented courtesy User-Agent format includes
contact info — we send a project identifier + repo URL. Operators
running their own deployment should set `SEC_EDGAR_USER_AGENT_EMAIL`
to a maintainer email so SEC ops can reach them if traffic spikes.
Empty falls back to a generic project identifier.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from cache.redis_cache import cache_get, cache_set
from config import settings

log = logging.getLogger(__name__)

# Recent-filings ATOM feed. Returns the latest `count` filings of
# the given form across all issuers. We stay at form='8-K' because
# 10-K / 10-Q have lower signal density (already-baked-in earnings)
# and arrive on a slower cadence that the cron's 1-hour cycle
# doesn't help with.
_EDGAR_RSS_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
_EDGAR_PARAMS = {
    "action": "getcurrent",
    "type": "8-K",
    "output": "atom",
    "count": "100",
}

# CIK→ticker mapping. Static-ish (changes when companies IPO/de-list)
# but we don't want to fetch on every cron tick. 24h Redis cache
# covers cross-pod consistency; in-process fallback covers Redis
# outages.
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_TICKER_MAP_CACHE_KEY = "sec_edgar:cik_to_ticker:v1"
_TICKER_MAP_TTL_SEC = 24 * 3600

_HTTP_TIMEOUT_S = 20.0

# ATOM namespace — `findall("{http://www.w3.org/2005/Atom}entry")`
# mirrors the namespace handling in `google_news_tw_connector.py`.
_ATOM_NS = "{http://www.w3.org/2005/Atom}"

# Title format: "8-K - COMPANY NAME (0000123456) (Filer)" — extract
# the zero-padded CIK from the parenthesised group. Tolerant of
# spacing changes by allowing flexible whitespace.
_CIK_RE = re.compile(r"\((\d{10})\)")

# In-process cache for the CIK map. Persists across cron ticks
# within one process; Redis covers cross-process consistency.
_TICKER_MAP_PROCESS_CACHE: dict[str, str] | None = None


@dataclass(frozen=True)
class FilingRow:
    """Canonical shape the cron consumes. Mirrors
    `mops_announcements_connector.AnnouncementRow` field-for-field
    so the cron's persistence path is identical for both markets."""
    market: str        # always "US" for SEC sourcing
    symbol: str
    announced_at: datetime
    category: str
    title: str
    body: str | None
    source_url: str | None
    source: str        # "sec_edgar_8k"

    @property
    def dedup_hash(self) -> str:
        return _hash_row(self.title, self.source_url or "", self.symbol)


def _hash_row(title: str, link: str, symbol: str) -> str:
    payload = "|".join([
        (symbol or "").strip(),
        " ".join((title or "").split()),
        (link or "").strip(),
    ]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _user_agent() -> str:
    """SEC-compliant User-Agent. Operator-supplied email lands in
    the second field per https://www.sec.gov/os/accessing-edgar-data —
    SEC ops emails the listed contact when a deployment misbehaves."""
    contact = (
        getattr(settings, "SEC_EDGAR_USER_AGENT_EMAIL", "") or "ops@example.com"
    )
    return f"FinceptWeb/1.0 ({contact}) +https://github.com/x812033727/FinceptWeb"


def _default_headers() -> dict[str, str]:
    return {
        "User-Agent": _user_agent(),
        "Accept": "application/atom+xml, text/xml, */*",
        # SEC requires the "Host" header on some endpoints; httpx
        # auto-sets this from the URL but spelling it out keeps the
        # behaviour explicit if a future code change swaps to a
        # CDN-fronted endpoint.
        "Accept-Encoding": "gzip, deflate",
    }


def _parse_announced_at(raw: str | None) -> datetime | None:
    """ATOM `<updated>` is ISO 8601 (always UTC, ends in 'Z' or
    explicit `+00:00`). Returns timezone-aware UTC datetime or None
    on parse failure (caller drops the row)."""
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        # `Z` → `+00:00` for fromisoformat (Python 3.10 doesn't
        # accept the bare 'Z' suffix; 3.11+ does, but be defensive).
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _extract_cik(title: str) -> str | None:
    """Pull the 10-digit zero-padded CIK out of the title's first
    parenthesised group. Returns None when the title doesn't match
    the expected shape (which we treat as a parse error so the cron
    bumps err_count + drops the row rather than guessing)."""
    if not title:
        return None
    m = _CIK_RE.search(title)
    return m.group(1) if m else None


def _strip_padding(cik: str) -> str:
    """SEC's company_tickers.json keys CIKs as integers (no padding);
    EDGAR feeds return them zero-padded to 10. Normalise both sides
    to the unpadded form for the lookup."""
    return cik.lstrip("0") or "0"


# ── CIK → ticker mapping ────────────────────────────────────────


async def _fetch_ticker_map_from_sec() -> dict[str, str] | None:
    """One-off fetch of the SEC's full CIK→ticker map. ~13 000 rows,
    ~600 KB. Returns None on any HTTP / parse failure — caller falls
    back to whatever the in-process cache holds (which may be
    nothing on a cold start, in which case the cron bumps err_count
    for every row but doesn't crash)."""
    try:
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT_S, headers=_default_headers(),
        ) as client:
            resp = await client.get(_TICKERS_URL)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning(
            "sec_edgar.ticker_map_fetch_failed",
            extra={"error": str(exc)},
        )
        return None

    out: dict[str, str] = {}
    if isinstance(payload, dict):
        # Format: {"0": {"cik_str": 320193, "ticker": "AAPL", ...}, ...}
        for entry in payload.values():
            if not isinstance(entry, dict):
                continue
            cik_int = entry.get("cik_str")
            ticker = entry.get("ticker")
            if isinstance(cik_int, int) and isinstance(ticker, str):
                out[str(cik_int)] = ticker.upper()
    return out


async def get_cik_ticker_map() -> dict[str, str]:
    """Resolve the CIK→ticker map. Three-tier:

      1. In-process cache (persists across cron ticks within one
         process; rebuilt on first miss).
      2. Redis cache (24h TTL, cross-pod).
      3. Fresh fetch from SEC.

    Returns an empty dict on a complete miss + Redis outage + SEC
    failure — the cron uses `.get(cik)` so an empty map gracefully
    drops every row of that tick rather than crashing.
    """
    global _TICKER_MAP_PROCESS_CACHE
    if _TICKER_MAP_PROCESS_CACHE is not None:
        return _TICKER_MAP_PROCESS_CACHE

    cached = await cache_get(_TICKER_MAP_CACHE_KEY)
    if cached:
        try:
            payload = json.loads(cached)
            if isinstance(payload, dict):
                _TICKER_MAP_PROCESS_CACHE = {
                    str(k): str(v) for k, v in payload.items()
                }
                return _TICKER_MAP_PROCESS_CACHE
        except (TypeError, ValueError):
            log.warning("sec_edgar.ticker_map_cache_corrupt")

    fetched = await _fetch_ticker_map_from_sec()
    if not fetched:
        return {}

    _TICKER_MAP_PROCESS_CACHE = fetched
    try:
        await cache_set(
            _TICKER_MAP_CACHE_KEY, json.dumps(fetched), _TICKER_MAP_TTL_SEC,
        )
    except Exception as exc:
        log.debug(
            "sec_edgar.ticker_map_cache_write_failed", extra={"error": str(exc)},
        )
    return fetched


# ── ATOM parsing ────────────────────────────────────────────────


def _normalize_entry(
    entry: ET.Element, *, cik_to_ticker: dict[str, str],
) -> FilingRow | None:
    """Convert one ATOM <entry> to a FilingRow. Returns None on any
    missing-required-field condition so the caller can bump
    err_count + continue."""
    title_el = entry.find(f"{_ATOM_NS}title")
    title = (title_el.text or "").strip() if title_el is not None else ""
    if not title:
        return None

    cik_padded = _extract_cik(title)
    if not cik_padded:
        return None
    ticker = cik_to_ticker.get(_strip_padding(cik_padded))
    if not ticker:
        # Issuer doesn't have a US-listed ticker (foreign filer,
        # private SPV). Drop silently — these rows wouldn't be
        # useful to the discussion ctx.
        return None

    updated_el = entry.find(f"{_ATOM_NS}updated")
    announced_at = _parse_announced_at(
        updated_el.text if updated_el is not None else None,
    )
    if announced_at is None:
        return None

    link_el = entry.find(f"{_ATOM_NS}link")
    source_url = link_el.get("href") if link_el is not None else None

    summary_el = entry.find(f"{_ATOM_NS}summary")
    body = (summary_el.text or "").strip() if summary_el is not None else None
    if body is not None and not body:
        body = None

    return FilingRow(
        market="US",
        symbol=ticker,
        announced_at=announced_at,
        # Item-code extraction (e.g. "Item 2.02") needs a follow-up
        # fetch of the filing index — out of scope for v1. Marking
        # the category as the bare form means the persona still sees
        # the disclosure type without us double-spending on SEC calls
        # per filing.
        category="8-K",
        title=title[:1000],
        body=body[:4000] if body else None,
        source_url=source_url,
        source="sec_edgar_8k",
    )


def _iter_entries(payload: bytes) -> Iterable[ET.Element]:
    """Yield ATOM <entry> elements from raw bytes. Tolerates parse
    errors by yielding nothing (caller treats as empty result, which
    is the same shape as a holiday with zero filings)."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        log.warning(
            "sec_edgar.atom_parse_failed", extra={"error": str(exc)},
        )
        return
    for entry in root.findall(f"{_ATOM_NS}entry"):
        yield entry


# ── public API ──────────────────────────────────────────────────


async def get_recent_8k_filings(
    *, limit: int = 100,
) -> tuple[list[FilingRow], int]:
    """Fetch + parse the recent 8-K filings.

    Returns `(rows, err_count)` — same contract as
    `mops_announcements_connector.get_recent_announcements`. Rows
    are the successfully-mapped FilingRows; err_count is the number
    of ATOM entries we had to drop because of missing/unrecognised
    fields. The cron surfaces both values in its health row so
    operators can spot "SEC changed the title format" or "ticker
    map cache stale" before it silently degrades to zero.

    Raises `httpx.HTTPError` on transport failure — same contract
    as the MOPS connector, lets the cron's error formatter render
    the SEC HTTP code into the health row.
    """
    cik_to_ticker = await get_cik_ticker_map()
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT_S, headers=_default_headers(),
    ) as client:
        resp = await client.get(_EDGAR_RSS_URL, params=_EDGAR_PARAMS)
    resp.raise_for_status()
    payload_bytes = resp.content

    rows: list[FilingRow] = []
    err_count = 0
    for entry in _iter_entries(payload_bytes):
        try:
            row = _normalize_entry(entry, cik_to_ticker=cik_to_ticker)
        except Exception as exc:
            log.debug(
                "sec_edgar.entry_parse_failed", extra={"error": str(exc)},
            )
            err_count += 1
            continue
        if row is None:
            err_count += 1
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows, err_count


def _reset_caches_for_tests() -> None:
    """Test-only — reset the in-process ticker-map cache so a
    monkeypatched `get_cik_ticker_map` is observable. Production
    code never calls this."""
    global _TICKER_MAP_PROCESS_CACHE
    _TICKER_MAP_PROCESS_CACHE = None


__all__ = [
    "FilingRow",
    "get_recent_8k_filings",
    "get_cik_ticker_map",
]
