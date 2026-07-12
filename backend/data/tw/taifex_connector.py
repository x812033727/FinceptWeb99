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


# ── Daily futures / options market reports ───────────────────────
#
# TAIFEX serves the full day's cross-section as a Big5 CSV behind a
# form POST (the "歷史交易資料下載" button). Unlike VIX these carry
# every contract + expiry + a day/after-hours session column. The
# finmind-clone tables (tw_futures_daily / tw_option_daily) key on
# (contract[,strike,call_put], ts) with NO expiry axis, so we keep the
# **near-month day session** row per key — the standard continuous
# front-contract series a chart wants. Emitted in FinMind's raw column
# names so the existing DatasetMapping.column_map projects them.

_FUT_DOWNLOAD_URL = "https://www.taifex.com.tw/cht/3/futDataDown"
_OPT_DOWNLOAD_URL = "https://www.taifex.com.tw/cht/3/optDataDown"

# Day session label in the 交易時段 column (vs 盤後 = after-hours).
_DAY_SESSION = "一般"


def _num(v: str | None) -> str | None:
    """TAIFEX cell → clean numeric string (or None). '-' / '' are the
    upstream missing-value sentinels; commas are thousands separators.
    Returns a str (not float) so the mapping's `_to_decimal`/`_to_int`
    coercion keeps full precision downstream."""
    if v is None:
        return None
    s = v.strip().replace(",", "")
    if s in ("", "-"):
        return None
    return s


def _parse_market_rows(body: str) -> list[dict[str, str]]:
    """Parse a TAIFEX market CSV into a list of header-keyed dicts (one
    per data row). Tolerant of trailing empty columns; strips cell
    whitespace. Returns [] on empty / header-only body."""
    if not body or not body.strip():
        return []
    rows = list(csv.reader(io.StringIO(body)))
    if len(rows) < 2:
        return []
    header = [h.strip() for h in rows[0]]
    out: list[dict[str, str]] = []
    for line in rows[1:]:
        if not any(c.strip() for c in line):
            continue
        out.append({
            header[i]: (line[i] if i < len(line) else "")
            for i in range(len(header))
        })
    return out


def _near_month_day_rows(
    rows: list[dict[str, str]], key_cols: tuple[str, ...],
) -> list[dict[str, str]]:
    """Keep the day-session ('一般') row with the smallest expiry per
    `key_cols` group — the near-month continuous contract. TAIFEX lists
    expiries ascending, so lexical-min of 到期月份(週別) is the front
    month (monthly '202601' sorts before same-month weekly '202601W1')."""
    best: dict[tuple, dict[str, str]] = {}
    for r in rows:
        if r.get("交易時段", "").strip() != _DAY_SESSION:
            continue
        expiry = r.get("到期月份(週別)", "").strip()
        key = tuple(r.get(c, "").strip() for c in key_cols)
        cur = best.get(key)
        if cur is None or expiry < cur["到期月份(週別)"].strip():
            best[key] = r
    return list(best.values())


async def _download_taifex_csv(url: str, referer: str, form: dict) -> str:
    headers = {"User-Agent": _USER_AGENT, "Referer": referer}
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        r = await client.post(url, data=form, headers=headers)
        r.raise_for_status()
    return r.content.decode(_TAIFEX_CSV_ENCODING, errors="replace")


async def get_futures_daily(
    start: date, end: date, commodity_id: str = "all",
) -> list[dict[str, Any]]:
    """Daily futures OHLCV for every contract over [start, end], near-
    month day session only. Rows in FinMind `TaiwanFuturesDaily` raw
    shape: {date, futures_id, open, max, min, close, volume,
    open_interest, settlement_price}. `commodity_id='all'` = every
    product (TAIFEX returns header-only for an empty id, so 'all' is the
    correct market-wide sentinel)."""
    if start > end:
        return []
    body = await _download_taifex_csv(
        _FUT_DOWNLOAD_URL, "https://www.taifex.com.tw/cht/3/futDailyMarketReport",
        {
            "down_type": "1",
            "queryStartDate": _format_taifex_date(start),
            "queryEndDate": _format_taifex_date(end),
            "commodity_id": commodity_id or "all",
        },
    )
    rows = _parse_market_rows(body)
    out: list[dict[str, Any]] = []
    # Group by (契約, 交易日期) so each trading day keeps its own near
    # month rather than one near-month across the whole window.
    for r in _near_month_day_rows(rows, ("契約", "交易日期")):
        d = _parse_taifex_day(r.get("交易日期", ""))
        if d is None:
            continue
        out.append({
            "date": d,
            "futures_id": r.get("契約", "").strip(),
            "open": _num(r.get("開盤價")),
            "max": _num(r.get("最高價")),
            "min": _num(r.get("最低價")),
            "close": _num(r.get("收盤價")),
            "volume": _num(r.get("成交量")),
            "open_interest": _num(r.get("未沖銷契約數")),
            "settlement_price": _num(r.get("結算價")),
        })
    return out


# FinMind's TaiwanOptionDaily call_put labels (Chinese, matching the
# TAIFEX source) — passed through so a Phase-A→B value diff stays clean.
_OPT_CP = {"買權": "買權", "賣權": "賣權"}


async def get_option_daily(
    start: date, end: date, commodity_id: str = "all",
) -> list[dict[str, Any]]:
    """Daily option OHLCV over [start, end], near-month day session per
    (contract, strike, call_put). Rows in FinMind `TaiwanOptionDaily`
    raw shape: {date, option_id, strike_price, call_put, open, max, min,
    close, volume, open_interest}. `commodity_id='all'` = every option
    series (~50 products)."""
    if start > end:
        return []
    body = await _download_taifex_csv(
        _OPT_DOWNLOAD_URL, "https://www.taifex.com.tw/cht/3/optDailyMarketReport",
        {
            "down_type": "1",
            "queryStartDate": _format_taifex_date(start),
            "queryEndDate": _format_taifex_date(end),
            "commodity_id": commodity_id or "all",
        },
    )
    rows = _parse_market_rows(body)
    out: list[dict[str, Any]] = []
    for r in _near_month_day_rows(
        rows, ("契約", "履約價", "買賣權", "交易日期"),
    ):
        d = _parse_taifex_day(r.get("交易日期", ""))
        if d is None:
            continue
        out.append({
            "date": d,
            "option_id": r.get("契約", "").strip(),
            "strike_price": _num(r.get("履約價")),
            "call_put": _OPT_CP.get(r.get("買賣權", "").strip()),
            "open": _num(r.get("開盤價")),
            "max": _num(r.get("最高價")),
            "min": _num(r.get("最低價")),
            "close": _num(r.get("收盤價")),
            "volume": _num(r.get("成交量")),
            "open_interest": _num(r.get("未沖銷契約數")),
        })
    return out


# ── Three-major-investor futures positions (三大法人-期貨) ─────────
#
# TAIFEX publishes one row per (date, 商品名稱, 身份別); FinMind's
# TaiwanFuturesInstitutionalInvestors is ONE wide row per (contract,
# date) with foreign / trust / dealer long-and-short OI as columns. So
# the handler PIVOTS the three 身份別 rows of a product into one wide
# row, mapping the Chinese product name to FinMind's futures_id code.

_FUT_INST_URL = "https://www.taifex.com.tw/cht/3/futContractsDateDown"

# 身份別 → pivot slot.
_INVESTOR_SLOT = {
    "外資及陸資": "foreign_investment",
    "外資": "foreign_investment",
    "投信": "investment_trust",
    "自營商": "dealer",
}

# 商品名稱 → FinMind futures_id. Curated for the headline index/sector
# futures (the ones used for 外資部位 directional analysis). Products
# not in this map are skipped rather than emitted under a Chinese name
# that wouldn't match FinMind on cutover; extend as needed and reconcile
# with `dry_run_cutover --values`.
_FUT_NAME_TO_ID = {
    "臺股期貨": "TX",
    "小型臺指期貨": "MTX",
    "微型臺指期貨": "TMF",
    "電子期貨": "TE",
    "小型電子期貨": "ZEF",
    "金融期貨": "TF",
    "非金電期貨": "XIF",
    "臺灣50期貨": "T5F",
}


async def get_futures_institutional(
    start: date, end: date,
) -> list[dict[str, Any]]:
    """三大法人-期貨 daily open-interest positions, pivoted to FinMind's
    `TaiwanFuturesInstitutionalInvestors` raw shape (one wide row per
    contract per day). Only the curated index/sector futures in
    `_FUT_NAME_TO_ID` are emitted."""
    if start > end:
        return []
    body = await _download_taifex_csv(
        _FUT_INST_URL, "https://www.taifex.com.tw/cht/3/futContractsDate",
        {
            "queryStartDate": _format_taifex_date(start),
            "queryEndDate": _format_taifex_date(end),
        },
    )
    rows = _parse_market_rows(body)
    # (date, futures_id) → wide row being assembled.
    wide: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        d = _parse_taifex_day(r.get("日期", ""))
        fid = _FUT_NAME_TO_ID.get(r.get("商品名稱", "").strip())
        slot = _INVESTOR_SLOT.get(r.get("身份別", "").strip())
        if d is None or fid is None or slot is None:
            continue
        key = (d, fid)
        wide.setdefault(key, {"date": d, "futures_id": fid})
        wide[key][f"long_open_interest_balance_volume_{slot}"] = _num(
            r.get("多方未平倉口數"))
        wide[key][f"short_open_interest_balance_volume_{slot}"] = _num(
            r.get("空方未平倉口數"))
    return list(wide.values())


def _parse_taifex_day(raw: str) -> str | None:
    """TAIFEX daily reports use Gregorian YYYY/MM/DD. Returns an ISO
    date string, or None when unparseable (holiday header rows etc.)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y/%m/%d").date().isoformat()
    except ValueError:
        return None


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


__all__ = [
    "get_vix_history", "get_futures_daily", "get_option_daily",
    "get_futures_institutional",
]
