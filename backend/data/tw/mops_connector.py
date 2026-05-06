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


_DEFAULT_TIMEOUT = 12.0
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    # The MOPS backend doesn't validate Origin/Referer but keeping
    # them in line with what the SPA sends helps if they tighten up.
    "Origin": "https://mops.twse.com.tw",
    "Referer": "https://mops.twse.com.tw/",
    "User-Agent": "FinceptWeb/1.0 (+https://github.com/x812033727/FinceptWeb)",
}


async def _mops_post(
    endpoint: str, payload: dict[str, Any], *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any] | None:
    """POST `payload` to `<MOPS API base>/<endpoint>` and return the
    parsed JSON `result` block, or None on any of:

      - HTTP error (transport / 4xx / 5xx)
      - body that isn't valid JSON
      - upstream `code` indicating "no data" / failure (typically 406
        for newly-listed companies that don't have a publication yet)

    Centralises the headers + error-handling so each new statement
    fetcher is just a thin wrapper. Per-symbol failures must NEVER
    raise upwards — the cron iterates 1700+ symbols and one bad row
    must not poison the whole batch.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(
                f"{_MOPS_API_BASE}/{endpoint}",
                json=payload, headers=_HEADERS,
            )
        if r.status_code != 200:
            log.warning("mops.http_error",
                        extra={"endpoint": endpoint, "status": r.status_code,
                               "payload": payload})
            return None
        body = r.json()
    except httpx.HTTPError as exc:
        log.warning("mops.transport_error",
                    extra={"endpoint": endpoint, "error": str(exc)})
        return None
    except ValueError:
        log.warning("mops.json_decode_failed",
                    extra={"endpoint": endpoint, "payload": payload})
        return None

    if not isinstance(body, dict):
        return None
    code = body.get("code")
    if code != 200 and code != "200":
        # 406 = "查無相符資料" for newly-listed / delisted symbols. Not
        # an error — just no data — so log at info, return None for
        # the wrapper to translate into an empty list.
        if code not in (406, "406"):
            log.info("mops.upstream_code",
                     extra={"endpoint": endpoint, "code": code,
                            "message": body.get("message")})
        return None

    result = body.get("result")
    if not isinstance(result, dict | list):
        return None
    return result if isinstance(result, dict) else {"data": result}


def _extract_rows(result: Any) -> list[dict[str, Any]]:
    """MOPS wraps row arrays differently across endpoints: sometimes
    `result.data`, sometimes `result.rows`, sometimes a flat list at
    `result` itself. Tolerate every observed shape."""
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    if isinstance(result, dict):
        rows = result.get("data") or result.get("rows") or result.get("list") or []
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    return []


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
    symbol: str, *, timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Pull the most recent monthly-revenue history for a single
    listed company from the MOPS new-SPA backend.

    Empty `month` + `year` asks the server for whatever it considers
    "recent" — typically the trailing 12-24 months. Cheaper than
    calling once per (symbol, year, month) tuple.

    Returns a list of `{symbol, date, revenue, revenue_yoy,
    revenue_mom}` rows. Empty list on every transport / upstream
    failure mode (handled by the shared `_mops_post` helper).
    """
    result = await _mops_post(
        "t05st10_ifrs",
        {
            "companyId": str(symbol).strip(),
            "dataType": "1",      # 1 = IFRS unified format
            "month": "",          # empty = server picks recent range
            "year": "",
            "subsidiaryCompanyId": "",
        },
        timeout=timeout,
    )
    if result is None:
        return []
    rows = _extract_rows(result)
    out: list[dict[str, Any]] = []
    for raw in rows:
        normalized = _normalize_row(symbol, raw)
        if normalized is not None:
            out.append(normalized)
    return out


# ── Quarterly statements (Phase 2A) ────────────────────────────────
#
# The new MOPS SPA backend serves each statement type via a separate
# JSON POST endpoint:
#   t164sb04_ifrs — 綜合損益表 (income statement)
#   t164sb03_ifrs — 資產負債表 (balance sheet)
#   t164sb05_ifrs — 現金流量表 (cash flow statement)
#
# Each takes `(companyId, year, season)` where `season ∈ {1, 2, 3, 4}`
# (Q1=1 ... Q4=4). Year is published-year (CE), so 2026 Q1 = the
# statement filed in Mar-May 2026 covering the Jan-Mar 2026 quarter.
#
# Output of every fetcher is a flat list of FinMind-shaped rows:
#   {stock_id, date, type, value}
# where `type` is the canonical English line-item name (Revenue,
# OperatingIncome, ...) that the runner's `_pivot_*` batch_transform
# expects. The pivot groups by (stock_id, date) and projects to the
# typed columns + raw JSONB blob — see mappings.py:_pivot_statement.

# Chinese row-label → English canonical type. Only the headline
# line-items are mapped (the rest land in `raw` JSONB anyway via
# the pivot's per-line catch-all). MOPS sometimes interleaves
# Traditional and Simplified variants — list both where they occur.
_INCOME_LABEL_MAP: dict[str, str] = {
    "營業收入合計":      "OperatingRevenue",
    "營業收入":          "OperatingRevenue",
    "營業毛利":          "GrossProfit",
    "營業利益":          "OperatingIncome",
    "營業利益(損失)":    "OperatingIncome",
    "本期淨利":          "NetIncome",
    "本期淨利(淨損)":    "NetIncome",
    "母公司業主(淨利/損)": "NetIncomeAttributableToParent",
    "歸屬於母公司業主":  "NetIncomeAttributableToParent",
    "基本每股盈餘":      "BasicEPS",
    "基本每股盈餘(元)":  "BasicEPS",
}

_BALANCE_LABEL_MAP: dict[str, str] = {
    "資產總額":           "TotalAssets",
    "資產總計":           "TotalAssets",
    "負債總額":           "TotalLiabilities",
    "負債總計":           "TotalLiabilities",
    "權益總額":           "TotalEquity",
    "權益總計":           "TotalEquity",
    "歸屬於母公司業主之權益": "Equity",
    "現金及約當現金":     "CashAndCashEquivalents",
}

_CASHFLOW_LABEL_MAP: dict[str, str] = {
    "營業活動之淨現金流入(流出)": "CashFlowsFromOperatingActivities",
    "投資活動之淨現金流入(流出)": "CashFlowsFromInvestingActivities",
    "籌資活動之淨現金流入(流出)": "CashFlowsFromFinancingActivities",
    "營業活動之現金流量":         "CashFlowsFromOperatingActivities",
    "投資活動之現金流量":         "CashFlowsFromInvestingActivities",
    "籌資活動之現金流量":         "CashFlowsFromFinancingActivities",
}


def _quarter_to_iso_period_end(year: int, quarter: int) -> str | None:
    """`(2024, 1)` → `'2024-03-31'` (statement cutoff date). Returns
    None for invalid (year, quarter) tuples — caller drops the row."""
    if not 1 <= quarter <= 4:
        return None
    if year < 1900:  # treat <1900 as ROC; +1911
        year += 1911
    month = quarter * 3
    day = 31 if month in (3, 12) else 30
    return f"{year:04d}-{month:02d}-{day:02d}"


def _label_lookup(raw_row: dict[str, Any]) -> tuple[str, Any] | None:
    """Pull the (label, value) tuple from one MOPS statement row.

    MOPS rotates between several field-name conventions across
    endpoint revisions. Handle the documented ones — return None when
    a row is structurally unparseable so the caller can drop it."""
    label = (
        raw_row.get("itemName")
        or raw_row.get("name")
        or raw_row.get("title")
        or raw_row.get("項目")
        or raw_row.get("會計項目")
    )
    if not label:
        return None
    label = str(label).strip()
    if not label:
        return None
    # Per-period value field varies across endpoints. Common keys:
    #   `value` / `currentValue` / `amount` / `本期金額`
    value = (
        raw_row.get("value")
        if "value" in raw_row else None
    )
    if value is None:
        for k in ("currentValue", "amount", "本期金額", "金額", "currentPeriodAmount"):
            if k in raw_row and raw_row[k] not in (None, ""):
                value = raw_row[k]
                break
    return label, value


def _flatten_statement_rows(
    symbol: str, year: int, quarter: int,
    rows: list[dict[str, Any]], label_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Convert a MOPS statement's row list to the FinMind shape the
    runner's `_pivot_statement` expects:
        {stock_id, date, type, value}

    Each row in MOPS is one line-item (e.g. `營業收入合計`); we map
    its Chinese label to a canonical English `type` and emit one
    output row. Rows whose label isn't in `label_map` are STILL
    emitted under the original Chinese label — the pivot stores
    them in `raw` JSONB so ad-hoc queries can still reach them
    even when they aren't promoted to typed columns.
    """
    period_end = _quarter_to_iso_period_end(year, quarter)
    if period_end is None:
        return []
    out: list[dict[str, Any]] = []
    for raw in rows:
        pair = _label_lookup(raw)
        if pair is None:
            continue
        label, value = pair
        canonical = label_map.get(label, label)
        out.append({
            "stock_id": symbol,
            "date":     period_end,
            "type":     canonical,
            "value":    _to_float(value),
        })
    return out


async def get_income_statement(
    symbol: str, year: int, quarter: int, *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """綜合損益表 for one (symbol, year, quarter) tuple via MOPS API
    `t164sb04_ifrs`. Returns rows in FinMind shape so the runner's
    `_pivot_income_statement` projects unchanged.

    Empty list on any upstream failure / "no data" response — see
    `_mops_post` for the centralised error handling. Does NOT raise.
    """
    result = await _mops_post(
        "t164sb04_ifrs",
        {
            "companyId": str(symbol).strip(),
            "dataType":  "1",
            "year":      str(year),
            "season":    str(quarter),
            "subsidiaryCompanyId": "",
        },
        timeout=timeout,
    )
    if result is None:
        return []
    return _flatten_statement_rows(
        symbol, year, quarter, _extract_rows(result), _INCOME_LABEL_MAP,
    )


async def get_balance_sheet(
    symbol: str, year: int, quarter: int, *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """資產負債表 via MOPS API `t164sb03_ifrs`. Same shape contract
    as `get_income_statement`."""
    result = await _mops_post(
        "t164sb03_ifrs",
        {
            "companyId": str(symbol).strip(),
            "dataType":  "1",
            "year":      str(year),
            "season":    str(quarter),
            "subsidiaryCompanyId": "",
        },
        timeout=timeout,
    )
    if result is None:
        return []
    return _flatten_statement_rows(
        symbol, year, quarter, _extract_rows(result), _BALANCE_LABEL_MAP,
    )


async def get_cash_flow_statement(
    symbol: str, year: int, quarter: int, *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """現金流量表 via MOPS API `t164sb05_ifrs`. Same shape contract
    as `get_income_statement`."""
    result = await _mops_post(
        "t164sb05_ifrs",
        {
            "companyId": str(symbol).strip(),
            "dataType":  "1",
            "year":      str(year),
            "season":    str(quarter),
            "subsidiaryCompanyId": "",
        },
        timeout=timeout,
    )
    if result is None:
        return []
    return _flatten_statement_rows(
        symbol, year, quarter, _extract_rows(result), _CASHFLOW_LABEL_MAP,
    )
