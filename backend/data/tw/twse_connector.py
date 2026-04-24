"""
TWSE OpenAPI connector.
Base: https://openapi.twse.com.tw/v1/
Rate limit: ~1 req/sec enforced via asyncio.Semaphore.
All responses are JSON. Timestamps converted to UTC before returning.
"""
import asyncio
import httpx
from datetime import datetime, date, timedelta, timezone
from typing import Any

_BASE = "https://openapi.twse.com.tw/v1"
_TPEX_BASE = "https://www.tpex.org.tw/openapi/v1"

# Conservative rate limit — TWSE throttles above ~1 req/sec
_sem = asyncio.Semaphore(1)
_DELAY = 1.1  # seconds between releases


async def _get(url: str, params: dict | None = None) -> Any:
    async with _sem:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            data = r.json()
        await asyncio.sleep(_DELAY)
    return data


def _twse_date(d: date | None = None) -> str:
    """TWSE uses YYYYMMDD format."""
    return (d or date.today()).strftime("%Y%m%d")


def _to_utc8_ts(date_str: str) -> str:
    """Convert YYYY-MM-DD to ISO8601 UTC+8 midnight."""
    try:
        dt = datetime.strptime(date_str, "%Y/%m/%d")
    except ValueError:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return date_str
    # Taiwan is UTC+8, no DST
    from datetime import timezone as tz_
    import pytz
    tw = pytz.timezone("Asia/Taipei")
    return tw.localize(dt).isoformat()


# ── TWSE symbol lists ─────────────────────────────────────────────

async def get_all_twse_symbols() -> list[dict[str, Any]]:
    """All TWSE-listed stocks with basic info."""
    data = await _get(f"{_BASE}/exchangeReport/STOCK_DAY_ALL")
    return data if isinstance(data, list) else []


async def get_all_tpex_symbols() -> list[dict[str, Any]]:
    """All TPEx (上櫃) listed stocks."""
    data = await _get(f"{_TPEX_BASE}/mopsfin/listingCompany")
    return data if isinstance(data, list) else []


# ── Daily OHLCV ───────────────────────────────────────────────────

async def get_daily_ohlcv(symbol: str, query_date: date | None = None) -> list[dict[str, Any]]:
    """
    Single stock daily OHLCV for one month.
    TWSE returns the entire month's data for the given date.
    """
    data = await _get(
        f"{_BASE}/exchangeReport/STOCK_DAY",
        params={"response": "json", "date": _twse_date(query_date), "stockNo": symbol},
    )
    rows = data if isinstance(data, list) else []
    result = []
    for r in rows:
        try:
            # TWSE date format: "113/04/01" (ROC calendar year + 1911 = CE)
            roc_date = r.get("日期", "")
            parts = roc_date.split("/")
            ce_date = f"{int(parts[0]) + 1911}-{parts[1]}-{parts[2]}" if len(parts) == 3 else roc_date
            result.append({
                "time": ce_date,
                "open":   _tw_num(r.get("開盤價")),
                "high":   _tw_num(r.get("最高價")),
                "low":    _tw_num(r.get("最低價")),
                "close":  _tw_num(r.get("收盤價")),
                "volume": _tw_int(r.get("成交股數")),
            })
        except Exception:
            continue
    return result


def _tw_num(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def _tw_int(s: str | None) -> int:
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, TypeError):
        return 0


# ── Real-time quote (best available — TWSE is ~3-5 min delayed) ──

async def get_realtime_quote(symbol: str) -> dict[str, Any] | None:
    """
    Uses the MI_INDEX endpoint which has the latest available price.
    Returns None if symbol not found.
    """
    data = await _get(f"{_BASE}/exchangeReport/STOCK_DAY_ALL")
    if not isinstance(data, list):
        return None
    for row in data:
        if row.get("Code") == symbol or row.get("證券代號") == symbol:
            return {
                "symbol": symbol,
                "name_zh": row.get("Name") or row.get("證券名稱", ""),
                "close": _tw_num(row.get("收盤價") or row.get("ClosingPrice")),
                "change": _tw_num(row.get("漲跌價差") or row.get("Change")),
                "volume": _tw_int(row.get("成交股數") or row.get("TradeVolume")),
                "open": _tw_num(row.get("開盤價") or row.get("OpeningPrice")),
                "high": _tw_num(row.get("最高價") or row.get("HighestPrice")),
                "low": _tw_num(row.get("最低價") or row.get("LowestPrice")),
            }
    return None


# ── Institutional investors (法人買賣超) ──────────────────────────

async def get_institutional(query_date: date | None = None) -> list[dict[str, Any]]:
    """
    All stocks' institutional buy/sell for one date.
    Endpoint: /fund/T86
    """
    data = await _get(
        f"{_BASE}/fund/T86",
        params={"response": "json", "date": _twse_date(query_date), "selectType": "ALLBUT0999"},
    )
    rows = data if isinstance(data, list) else []
    result = []
    for r in rows:
        result.append({
            "symbol":       r.get("證券代號", "").strip(),
            "name_zh":      r.get("證券名稱", "").strip(),
            "fini_buy":     _tw_int(r.get("外陸資買進股數(不含外資自營商)")),
            "fini_sell":    _tw_int(r.get("外陸資賣出股數(不含外資自營商)")),
            "sitc_buy":     _tw_int(r.get("投信買進股數")),
            "sitc_sell":    _tw_int(r.get("投信賣出股數")),
            "dealer_buy":   _tw_int(r.get("自營商買進股數(自行買賣)")),
            "dealer_sell":  _tw_int(r.get("自營商賣出股數(自行買賣)")),
        })
    return result


# ── Margin balance (融資融券) ─────────────────────────────────────

async def get_margin(query_date: date | None = None) -> list[dict[str, Any]]:
    """Endpoint: /fund/MI_MARGN"""
    data = await _get(
        f"{_BASE}/fund/MI_MARGN",
        params={"response": "json", "date": _twse_date(query_date), "selectType": "MS"},
    )
    rows = data if isinstance(data, list) else []
    result = []
    for r in rows:
        result.append({
            "symbol":          r.get("股票代號", "").strip(),
            "name_zh":         r.get("名稱", "").strip(),
            "margin_purchase": _tw_int(r.get("融資買進")),
            "margin_balance":  _tw_int(r.get("融資餘額")),
            "short_sale":      _tw_int(r.get("融券賣出")),
            "short_balance":   _tw_int(r.get("融券餘額")),
        })
    return result


# ── TAIEX index ───────────────────────────────────────────────────

async def get_taiex() -> dict[str, Any]:
    data = await _get(f"{_BASE}/indicesReport/MI_5MINS")
    if isinstance(data, list) and data:
        latest = data[-1]
        return {
            "index": "TAIEX",
            "value": _tw_num(latest.get("指數")),
            "change": _tw_num(latest.get("漲跌點數")),
            "time": latest.get("時間"),
        }
    return {}


# ── Valuation ratios: PE, PB, dividend yield ─────────────────────

async def get_valuation_ratios(symbol: str) -> dict[str, Any]:
    """
    本益比、股價淨值比、殖利率 from TWSE BWIBBU_d endpoint.
    Returns the most recent available record for `symbol`.
    """
    data = await _get(
        "https://www.twse.com.tw/exchangeReport/BWIBBU_d",
        params={"response": "json", "stockNo": symbol},
    )
    rows: list = []
    if isinstance(data, dict):
        rows = data.get("data", []) or data.get("Data", [])
    elif isinstance(data, list):
        rows = data

    if not rows:
        return {}

    # Rows: [日期, 殖利率(%), 股利年度, 本益比, 股價淨值比]
    last = rows[-1]
    try:
        return {
            "pe_ratio":       _tw_num(last[3]) if len(last) > 3 else None,
            "pb_ratio":       _tw_num(last[4]) if len(last) > 4 else None,
            "dividend_yield": _tw_num(last[1]) if len(last) > 1 else None,
            "fetched_at":     last[0] if last else None,
        }
    except Exception:
        return {}
