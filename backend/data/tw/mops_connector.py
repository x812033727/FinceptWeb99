"""
公開資訊觀測站 (MOPS) connector — monthly revenue per symbol.

The legacy `mops.twse.com.tw/mops/web/ajax_t05st10_ifrs` endpoint
(scraped HTML) was security-blocked some time in 2025/26: every request
returns the "FOR SECURITY REASONS, THIS PAGE CAN NOT BE ACCESSED" page
regardless of headers / referer. The new MOPS SPA fetches from a
separate API host (`mops.interinfo.com.tw:8443/mops/api/`) which still
serves JSON to anonymous callers — that's what we use here.

The backend doesn't expose a market-wide one-call download (the old
`t21sc03` summary report was retired during the SPA rewrite), so the
ingest path is per-symbol with cron-paced fan-out — see
`tasks.ingest_revenue_tw_slow` for the scheduler half.

Revenue values are in 千元 NTD (thousands of NTD); growth percentages
are already in percent units (e.g. 12.5 means +12.5 %).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

_MOPS_API_BASE = "https://mops.interinfo.com.tw:8443/mops/api"


def _to_int(v: Any) -> int | None:
    """MOPS returns numbers as strings with thousand separators
    (`"1,234,567"`). Empty / None / `"--"` / `"-"` mean "no data"."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "--", "N/A"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "--", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _normalize_row(symbol: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a single MOPS API result row to the canonical
    `{symbol, date, revenue, revenue_yoy, revenue_mom}` shape used by
    `services.ingest.repository.RevenueMonthlyRow` and the rest of the
    pipeline.

    Tolerant against the multiple field-name conventions MOPS has
    rotated through. Returns None when the row doesn't carry a
    parseable date — better to drop one row than poison the upsert.
    """
    # Year + month → ISO date (first of month).
    yr = raw.get("year") or raw.get("yyyy") or raw.get("revenueYear")
    mo = raw.get("month") or raw.get("mm") or raw.get("revenueMonth")
    # Some payloads use a combined "yymm" / "ym" / "yearMonth" key
    # (e.g. "11404" = ROC 114 / 04 = 2025-04 CE).
    if (yr is None or mo is None) and (raw.get("yymm") or raw.get("yearMonth")):
        ym = str(raw.get("yymm") or raw.get("yearMonth")).strip()
        if len(ym) >= 5 and ym.isdigit():
            yr, mo = ym[:-2], ym[-2:]
    if yr is None or mo is None:
        return None
    try:
        yr_int = int(str(yr).strip())
        mo_int = int(str(mo).strip())
    except ValueError:
        return None
    # ROC → CE if the year looks like a 3-digit ROC year (typical:
    # 100-199). Anything ≥ 1900 is already CE.
    if yr_int < 1900:
        yr_int += 1911
    if not (1 <= mo_int <= 12):
        return None
    iso_date = f"{yr_int}-{mo_int:02d}-01"

    # Revenue itself — try common field names.
    revenue = _to_int(
        raw.get("revenue")
        or raw.get("currentRevenue")
        or raw.get("currentMonthRevenue")
        or raw.get("本月營收")
    )
    yoy = _to_float(
        raw.get("revenueYoy")
        or raw.get("yoy")
        or raw.get("yoyPct")
        or raw.get("yoyChange")
        or raw.get("去年同月增減百分比")
    )
    mom = _to_float(
        raw.get("revenueMom")
        or raw.get("mom")
        or raw.get("momPct")
        or raw.get("momChange")
        or raw.get("上月比較增減百分比")
    )

    return {
        "symbol":      symbol,
        "date":        iso_date,
        "revenue":     revenue,           # 千元 NTD
        "revenue_yoy": yoy,
        "revenue_mom": mom,
    }


async def get_monthly_revenue_recent(
    symbol: str, *, timeout: float = 12.0,
) -> list[dict[str, Any]]:
    """Pull the most recent monthly-revenue history for a single
    listed company from the MOPS new-SPA backend.

    Empty `month` + `year` asks the server for whatever it considers
    "recent" — typically the trailing 12-24 months. Cheaper than
    calling once per (symbol, year, month) tuple.

    Returns a list of `{symbol, date, revenue, revenue_yoy,
    revenue_mom}` rows. Empty list on:
      - HTTP error (connection / 4xx / 5xx)
      - MOPS `code` != 200 (e.g. 406 "查無相符資料" for newly-listed
        / delisted symbols)
      - Unparseable response body

    Raises only on truly catastrophic errors (e.g. the caller passed
    a non-string). Per-symbol failures must not abort the cron's
    batch over 1700+ symbols.
    """
    payload = {
        "companyId": str(symbol).strip(),
        "dataType": "1",      # 1 = IFRS unified format
        "month": "",          # empty = server picks recent range
        "year": "",
        "subsidiaryCompanyId": "",
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        # The MOPS backend doesn't validate Origin/Referer but keeping
        # them in line with what the SPA sends helps if they tighten up.
        "Origin": "https://mops.twse.com.tw",
        "Referer": "https://mops.twse.com.tw/",
        "User-Agent": "FinceptWeb/1.0 (+https://github.com/x812033727/FinceptWeb)",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(
                f"{_MOPS_API_BASE}/t05st10_ifrs",
                json=payload, headers=headers,
            )
        if r.status_code != 200:
            log.warning("mops.revenue.http_error",
                        extra={"symbol": symbol, "status": r.status_code})
            return []
        body = r.json()
    except httpx.HTTPError as exc:
        log.warning("mops.revenue.transport_error",
                    extra={"symbol": symbol, "error": str(exc)})
        return []
    except ValueError:
        # JSON decode failure — MOPS sometimes returns the SPA HTML
        # under load.
        log.warning("mops.revenue.json_decode_failed",
                    extra={"symbol": symbol})
        return []

    if not isinstance(body, dict):
        return []
    code = body.get("code")
    if code != 200 and code != "200":
        # 406 = "查無相符資料" is normal for newly-listed companies
        # without a publication yet; not an error.
        if code not in (406, "406"):
            log.info("mops.revenue.upstream_code",
                     extra={"symbol": symbol, "code": code,
                            "message": body.get("message")})
        return []

    result = body.get("result") or {}
    # MOPS can wrap the row list in `result.data` or `result.rows`
    # depending on the report variant; tolerate both + a flat list.
    rows: list[Any] = []
    if isinstance(result, list):
        rows = result
    elif isinstance(result, dict):
        rows = result.get("data") or result.get("rows") or []

    out: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        normalized = _normalize_row(symbol, raw)
        if normalized is not None:
            out.append(normalized)
    return out
