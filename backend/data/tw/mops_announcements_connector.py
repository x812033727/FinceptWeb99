"""TW MOPS material-info disclosure connector (PR-D1).

Pulls the daily 重大訊息 list from MOPS — the corporate-disclosure
counterpart to `google_news_tw_connector` (which pulls market news).
MOPS publishes one row per disclosure with structured `category`,
`stock_id`, `announced_at`, `title`, and a free-text `body`. The
ingest cron in `tasks/ingest_announcements_tw.py` calls
`get_recent_announcements()` every 30 min and dedups via sha256
on (title, link) so re-runs are idempotent.

Why MOPS over FinMind:

  - FinMind's catalogue (97 datasets) doesn't include 重大訊息;
    `TaiwanStockNews` is the closest cousin but is freeform news
    aggregation, not the official issuer disclosures we want.
  - MOPS's TWSE-hosted ajax endpoint is free + public + structured
    + reliably-categorised. Free tier: no token, no quota beyond
    the standard TWSE rate budget.

Endpoint: `https://mopsov.twse.com.tw/mops/web/ajax_t05st02`. The
SPA backend serves the same data via the newer
`mops.interinfo.com.tw:8443/mops/api` family but the legacy ajax
form is more stable across MOPS UI revisions. POST form-encoded;
returns JSON with a `data` array of disclosure rows.

The connector is intentionally tolerant — every shape change MOPS
introduces (column reorder, encoding, new optional fields) gets
flagged via per-row try/except + a structured `err_count` counter
the cron surfaces in the health row, NOT a hard failure that drops
the whole batch.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

_MOPS_ENDPOINT = "https://mopsov.twse.com.tw/mops/web/ajax_t05st02"

# Per-call HTTP timeout. MOPS responds in ~1-2 s on a normal day,
# but the ajax endpoint is occasionally slow during market open.
_HTTP_TIMEOUT_S = 20.0

# Some MOPS endpoints reject browser-less UAs with empty bodies.
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; FinceptWeb/1.0; "
        "+https://github.com/x812033727/FinceptWeb)"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    # Browser session cookie isn't required for ajax_t05st02 but
    # MOPS occasionally seeds one via the parent SPA. We don't
    # carry one — if MOPS starts requiring it, the cron will
    # surface a clean HTTP 403 in the health row.
}

# TW timezone offset for `announced_at` parsing — MOPS publishes
# in Asia/Taipei wall-clock without explicit tz suffix. Fixed
# +08:00 (TW doesn't observe DST).
_TPE = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class AnnouncementRow:
    """Canonical shape the cron consumes. Flat, no nested dicts —
    callers don't need to know about MOPS' field rotation."""
    market: str        # always "TW" for MOPS sourcing
    symbol: str
    announced_at: datetime
    category: str
    title: str
    body: str | None
    source_url: str | None
    source: str        # "mops_t05st02" — distinguishes from any
                       # future operator-supplied alternative source

    @property
    def dedup_hash(self) -> str:
        return _hash_row(self.title, self.source_url or "", self.symbol)


def _hash_row(title: str, link: str, symbol: str) -> str:
    """SHA256 over (symbol, normalized title, link). Symbol joins
    the digest so two issuers happening to file the same boilerplate
    title (法說會通知 etc.) on the same day each get their own row.
    """
    payload = "|".join([
        (symbol or "").strip(),
        " ".join((title or "").split()),
        (link or "").strip(),
    ]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _parse_announced_at(raw: Any) -> datetime | None:
    """MOPS publishes 民國 + ROC date + HH:MM. Tolerates several
    common shapes since the upstream rotates between
    `113/05/09 14:32` and `2026/05/09 14:32` depending on which
    column the SPA happens to render through. Returns timezone-
    aware datetime in Asia/Taipei (+08:00) on success; None on
    parse failure (caller drops the row + bumps err_count).
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    parts = s.split()
    if len(parts) < 2:
        return None
    date_part, time_part = parts[0], parts[1]
    try:
        if "/" in date_part:
            d_year, d_mon, d_day = date_part.split("/")
            year = int(d_year)
            # ROC (民國) — three-digit year < 200 is the giveaway.
            if year < 200:
                year += 1911
            mon = int(d_mon)
            day = int(d_day)
        else:
            return None
        hh, mm = time_part.split(":")[:2]
        return datetime(
            year, mon, day, int(hh), int(mm), tzinfo=_TPE,
        ).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _normalize_row(raw: dict[str, Any]) -> AnnouncementRow | None:
    """Convert one MOPS row to the canonical shape. Returns None for
    any malformed row so the caller can bump err_count and continue.

    MOPS uses several field-name conventions across endpoints — this
    accepts the union. The `data` payload is sometimes a
    `{key: value}` dict, sometimes an array of arrays with positional
    columns; the cron passes through `_extract_rows` first to flatten.
    """
    symbol = (
        raw.get("stock_id")
        or raw.get("symbol")
        or raw.get("co_id")
        or raw.get("companyId")
        or ""
    )
    if not isinstance(symbol, str):
        return None
    symbol = symbol.strip()
    if not symbol:
        return None

    announced_at = _parse_announced_at(
        raw.get("announce_date_time")
        or raw.get("date_time")
        or raw.get("announceTime")
        or raw.get("announce_dt")
    )
    if announced_at is None:
        return None

    title = (
        raw.get("subject")
        or raw.get("title")
        or raw.get("topic")
        or ""
    )
    if not isinstance(title, str):
        return None
    title = title.strip()
    if not title:
        return None

    category = (
        raw.get("type_k")
        or raw.get("type_name")
        or raw.get("category")
        or "未分類"
    )
    if not isinstance(category, str):
        category = "未分類"
    category = category.strip() or "未分類"

    body = (
        raw.get("content")
        or raw.get("description")
        or raw.get("body")
        or None
    )
    if body is not None and not isinstance(body, str):
        body = None

    source_url = (
        raw.get("link")
        or raw.get("url")
        or raw.get("source_url")
        or None
    )
    if source_url is not None and not isinstance(source_url, str):
        source_url = None

    return AnnouncementRow(
        market="TW",
        symbol=symbol,
        announced_at=announced_at,
        category=category[:64],
        title=title[:1000],
        body=body[:4000] if body else None,
        source_url=source_url,
        source="mops_t05st02",
    )


def _extract_rows(payload: Any) -> Iterable[dict[str, Any]]:
    """MOPS sometimes nests rows under `.data`, sometimes under
    `.result.rows`, sometimes is the array itself. Yield whatever
    looks row-shaped; non-dict items are skipped so a malformed
    payload doesn't cascade into a crash."""
    if isinstance(payload, list):
        for r in payload:
            if isinstance(r, dict):
                yield r
        return
    if not isinstance(payload, dict):
        return
    for key in ("data", "rows", "items"):
        sub = payload.get(key)
        if isinstance(sub, list):
            for r in sub:
                if isinstance(r, dict):
                    yield r
            return
    result = payload.get("result")
    if isinstance(result, dict):
        sub = result.get("rows") or result.get("data")
        if isinstance(sub, list):
            for r in sub:
                if isinstance(r, dict):
                    yield r


async def get_recent_announcements(
    *, limit: int = 200,
) -> tuple[list[AnnouncementRow], int]:
    """Fetch + parse today's MOPS material-info disclosures.

    Returns `(rows, err_count)` — rows are the successfully-parsed
    `AnnouncementRow`s; err_count is the number of MOPS rows we had
    to drop because of unrecognised shape / missing required field.
    The cron surfaces both values in its health row so operators
    can see "MOPS column rotation in flight, half the rows are
    being dropped" without staring at logs.

    Raises `httpx.HTTPError` on transport failure — the cron's
    error handler converts it into a structured failure with the
    HTTP status / hint preserved (mirroring `ingest_news_tw`).
    """
    body = {"step": "0", "TYPEK": "all", "co_id": "", "year": "", "month": ""}
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT_S, headers=_DEFAULT_HEADERS,
    ) as client:
        resp = await client.post(_MOPS_ENDPOINT, data=body)
    resp.raise_for_status()
    try:
        payload = resp.json()
    except ValueError:
        log.warning(
            "mops_announcements.non_json_response",
            extra={"snippet": resp.text[:200]},
        )
        return [], 0

    rows: list[AnnouncementRow] = []
    err_count = 0
    for raw in _extract_rows(payload):
        try:
            row = _normalize_row(raw)
        except Exception as exc:
            log.debug(
                "mops_announcements.row_parse_failed",
                extra={"error": str(exc)},
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


__all__ = [
    "AnnouncementRow",
    "get_recent_announcements",
]
