"""TAIFEX (台灣期貨交易所) public-data connector.

Currently exposes one endpoint: 「臺指選擇權波動率指數」(TAIWAN VIX)
historical CSV download. The dataset is free + public and not in
FinMind's catalogue (verified PR #283 — neither `TaiwanVIX` nor
similar names resolve), so we go straight to TAIFEX.

Why we keep this connector slim:
  - TAIFEX serves the CSV behind a form POST on a stable URL but
    occasionally rotates anti-scrape headers. Keeping the parser
    isolated here means a header rotation breaks ONE module
    instead of cascading through the discussion stack.
  - The CSV is only ~30 rows when we pull a 30-day window —
    no reason for a high-throughput parser.

Future expansion: 個股期貨 大額交易人未沖銷部位 + 選擇權 IV 曲線
都可以走同一個 connector。
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)


# Documented at https://www.taifex.com.tw/cht/3/vixQuote (the page
# behind the "歷史資料下載" button POSTs to this URL with the date
# range as form data).
_VIX_DOWNLOAD_URL = "https://www.taifex.com.tw/cht/3/vixDownload"

# TAIFEX serves Big5-encoded CSV. Pin so a future server-side
# locale flip doesn't silently mojibake the date column.
_TAIFEX_CSV_ENCODING = "big5"


# Rows we want to keep for downstream parsers — the CSV header
# columns we look for are documented as Chinese strings; map them
# to canonical English keys so the rest of the codebase doesn't
# carry encoded strings.
_VIX_DATE_HEADERS = ("日期",)
_VIX_VALUE_HEADERS = ("收盤指數", "收盤", "VIX", "波動率指數")


_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=30.0)
_USER_AGENT = (
    "Mozilla/5.0 (Linux; Server) FinceptWeb/0.5 "
    "(taifex-vix-ingest)"
)


def _format_taifex_date(d: date) -> str:
    """TAIFEX expects YYYY/MM/DD in form params — explicit zero-padding
    so a 1-digit month/day (April 5th) doesn't trigger a 400."""
    return d.strftime("%Y/%m/%d")


def _parse_csv_body(body: str) -> list[dict[str, Any]]:
    """Parse the TAIFEX VIX CSV into ``[{date, value}, ...]``.

    The CSV layout (snapshot 2026-05):
        日期,收盤指數
        2026/04/15,18.32
        2026/04/16,17.51
        ...

    Tolerant of column-order changes + extra columns (some TAIFEX
    pages include 開盤/最高/最低 alongside 收盤). We only look at
    the date + close columns by header name; everything else is
    ignored.

    Skips any row whose date doesn't parse OR whose value isn't a
    finite float. Empty header / mojibake'd input → returns [].
    """
    if not body or not body.strip():
        return []
    reader = csv.reader(io.StringIO(body))
    rows = list(reader)
    if len(rows) < 2:
        return []

    header = [h.strip() for h in rows[0]]
    date_idx = next(
        (i for i, h in enumerate(header) if h in _VIX_DATE_HEADERS),
        None,
    )
    value_idx = next(
        (i for i, h in enumerate(header) if h in _VIX_VALUE_HEADERS),
        None,
    )
    if date_idx is None or value_idx is None:
        log.warning(
            "taifex.vix.unrecognized_csv_header",
            extra={"header": header},
        )
        return []

    out: list[dict[str, Any]] = []
    for line in rows[1:]:
        if len(line) <= max(date_idx, value_idx):
            continue
        raw_date = (line[date_idx] or "").strip()
        raw_value = (line[value_idx] or "").strip().replace(",", "")
        if not raw_date or not raw_value:
            continue
        # TAIFEX writes ROC-year and Gregorian dates depending on
        # the page; this endpoint uses Gregorian YYYY/MM/DD.
        try:
            ts = datetime.strptime(raw_date, "%Y/%m/%d").date()
        except ValueError:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        out.append({"date": ts.isoformat(), "value": value})
    return out


async def get_vix_history(
    start: date, end: date,
) -> list[dict[str, Any]]:
    """Fetch the TAIWAN VIX (台指選擇權波動率指數) closes between
    ``start`` and ``end`` (inclusive). Empty list on connector
    failure or empty body — caller decides how to surface it
    (cron records ``ok=False`` with a diagnostic message; ctx
    block leaves the value as None).
    """
    if start > end:
        return []
    params = {
        "queryStartDate": _format_taifex_date(start),
        "queryEndDate":   _format_taifex_date(end),
    }
    headers = {
        "User-Agent": _USER_AGENT,
        "Referer":    "https://www.taifex.com.tw/cht/3/vixQuote",
    }
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        try:
            r = await client.post(
                _VIX_DOWNLOAD_URL, data=params, headers=headers,
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning(
                "taifex.vix.fetch_failed",
                extra={"error": str(exc), "start": str(start), "end": str(end)},
            )
            raise
    body = r.content.decode(_TAIFEX_CSV_ENCODING, errors="replace")
    return _parse_csv_body(body)


__all__ = ["get_vix_history"]
