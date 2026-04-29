"""
Taiwan market service — owns all caching and waterfall fallback logic.
Data source priority:
  quote/OHLCV    : TWSE OpenAPI → FinMind
  institutional  : TWSE OpenAPI → FinMind
  margin         : TWSE OpenAPI → FinMind
  monthly revenue: FinMind → MOPS scraper
  financials     : FinMind only

Timezone: Taiwan is UTC+8, no DST. All API responses are tagged with
tz="Asia/Taipei" so the frontend can display correct local labels.
"""
import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytz

import data.tw.finmind_connector as finmind
import data.tw.mops_connector as mops
import data.tw.twse_connector as twse
from cache.redis_cache import (
    cache_get,
    cache_set,
    key_history,
    key_institutional,
    key_margin,
    key_quote,
    key_revenue,
)

log = logging.getLogger(__name__)

_TW = pytz.timezone("Asia/Taipei")

TTL_QUOTE        = 60          # 1 min (TWSE is ~3-5 min delayed anyway)
TTL_HISTORY      = 4 * 3600
TTL_INSTITUTIONAL = 4 * 3600
TTL_MARGIN       = 4 * 3600
TTL_REVENUE      = 12 * 3600
TTL_FUNDAMENTALS = 24 * 3600
TTL_SCREENER     = 10 * 60

# In-process symbol→exchange map; refreshed by scheduler (Phase 5)
_exchange_map: dict[str, str] = {}   # symbol → "TWSE" | "TPEx"


# TW ETFs are 4–6-digit codes that start with "00" (e.g. 0050, 0056,
# 00713, 006208). Futures-based ETFs and 反向/槓桿 ETFs (00xxxR / 00xxxL)
# also fit this regex. Regular stocks are 4-digit codes 1xxx–9xxx.
_ETF_CODE = re.compile(r"^00\d{2,4}[A-Z]?$")


def is_etf(symbol: str) -> bool:
    return bool(_ETF_CODE.match(symbol or ""))


def _is_tw_market_open() -> bool:
    now = datetime.now(_TW)
    if now.weekday() >= 5:
        return False
    open_  = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
    close_ = now.replace(hour=13, minute=30, second=0, microsecond=0)
    return open_ <= now < close_


def _today_str() -> str:
    return date.today().isoformat()


def _start_date(months: int = 6) -> str:
    return (date.today() - timedelta(days=months * 30)).isoformat()


# ── Symbol exchange lookup ────────────────────────────────────────

def get_exchange(symbol: str) -> str:
    return _exchange_map.get(symbol, "TWSE")


async def refresh_symbol_map() -> None:
    """Called by scheduler daily. Builds symbol→exchange map."""
    global _exchange_map
    new_map: dict[str, str] = {}
    try:
        for row in await twse.get_all_twse_symbols():
            code = row.get("Code") or row.get("證券代號", "")
            if code:
                new_map[code.strip()] = "TWSE"
    except Exception:
        pass
    try:
        for row in await twse.get_all_tpex_symbols():
            code = row.get("SecuritiesCompanyCode") or row.get("代號", "")
            if code:
                new_map[code.strip()] = "TPEx"
    except Exception:
        pass
    if new_map:
        _exchange_map = new_map


# ── Quote ─────────────────────────────────────────────────────────

async def fetch_quote_waterfall(symbol: str) -> tuple[dict | None, str]:
    """Run the TWSE realtime → FinMind 7-day fallback waterfall.

    Returns (raw, source). Pulled out of get_quote() so the background
    WS-publish task can reuse the same fallback chain — without this the
    polling task only tried TWSE and gave up, freezing every subscriber's
    live price whenever TWSE OpenAPI hiccupped.
    """
    raw = None
    source = "unavailable"
    try:
        raw = await twse.get_realtime_quote(symbol)
        if raw:
            source = "twse"
    except Exception as exc:
        log.warning("tw.quote.twse_failed",
                    extra={"symbol": symbol, "error": str(exc)})

    if not raw:
        # FinMind fallback: latest close from last 5 days. This is end-of-day
        # data, not realtime — the UI flags it via data_source="finmind" so
        # the user knows they're looking at yesterday's close during market
        # hours.
        try:
            start = (date.today() - timedelta(days=7)).isoformat()
            bars = await finmind.get_daily_ohlcv(symbol, start)
            if bars:
                latest = bars[-1]
                raw = {
                    "symbol": symbol, "name_zh": "",
                    "close": latest["close"], "change": None,
                    "volume": latest["volume"],
                    "open": latest["open"], "high": latest["high"], "low": latest["low"],
                }
                source = "finmind"
        except Exception as exc:
            log.warning("tw.quote.finmind_failed",
                        extra={"symbol": symbol, "error": str(exc)})

    if not raw:
        log.warning("tw.quote.all_sources_failed", extra={"symbol": symbol})

    return raw, source


async def get_quote(symbol: str, *, bypass_cache: bool = False) -> dict[str, Any]:
    """Read TW quote. Hits Redis cache first unless `bypass_cache` is
    set — caller asks for fresh data when the user explicitly opens
    a view that promises live prices (e.g. watchlist page on mount).
    Cache TTL is 15 s so the bypass mostly matters off-hours when the
    cache otherwise persists for a full TTL."""
    from services.ingest.repository import read_latest_quote_autosession

    key = key_quote("tw", symbol)
    if not bypass_cache:
        cached = await cache_get(key)
        if cached:
            return json.loads(cached)

    raw, source = await fetch_quote_waterfall(symbol)
    if not raw:
        # ── Tier 3: recent DB snapshot when upstream is fully down ──
        # Refresh task writes one row per active symbol every minute, so
        # a 5-minute window catches at most a few stale ticks during a
        # transient TWSE+FinMind outage instead of returning blank state.
        snap = await read_latest_quote_autosession("TW", symbol, max_age_seconds=300)
        if snap is not None:
            result = _normalize_quote(symbol, {
                "close": snap["price"],
                "volume": snap.get("volume", 0),
                "prev_close": snap.get("prev_close"),
            })
            result["data_source"] = snap.get("data_source") or "db"
            result["change_pct"] = snap.get("change_pct")
            log.info("tw.quote.served_db_snapshot", extra={"symbol": symbol})
            return result

    result = _normalize_quote(symbol, raw or {})
    result["data_source"] = source
    # Don't cache the zero-state (TWSE + FinMind both failed) — keeps the
    # next request retrying instead of locking a 60-second blank quote.
    if result.get("price"):
        await cache_set(key, json.dumps(result), TTL_QUOTE)
    return result


def _normalize_quote(symbol: str, raw: dict) -> dict[str, Any]:
    close = raw.get("close") or raw.get("price", 0) or 0
    prev  = raw.get("prev_close", 0) or 0
    chg   = raw.get("change") if raw.get("change") is not None else (close - prev if prev else None)
    chg_pct = round(chg / prev * 100, 4) if (chg is not None and prev) else None
    return {
        "symbol":        symbol,
        "market":        "TW",
        "exchange":      get_exchange(symbol),
        "name_zh":       raw.get("name_zh", ""),
        "price":         close,
        "change":        chg,
        "change_pct":    chg_pct,
        "volume":        raw.get("volume", 0),
        "open":          raw.get("open"),
        "high":          raw.get("high"),
        "low":           raw.get("low"),
        "currency":      "TWD",
        "ts":            int(datetime.now(UTC).timestamp() * 1000),
        "tz":            "Asia/Taipei",
        "is_market_open": _is_tw_market_open(),
        "is_etf":        is_etf(symbol),
    }


# ── History ───────────────────────────────────────────────────────

# Bars from the DB read tier are considered "fresh enough" if their most
# recent ts is within this many calendar days of today. 5 days tolerates
# weekends + a typical TW national holiday without falling through to
# upstream unnecessarily.
_DB_HISTORY_FRESHNESS_DAYS = 5


def _db_bars_are_fresh(bars: list[dict[str, Any]], today: date) -> bool:
    if not bars:
        return False
    last = bars[-1].get("time")
    if not last:
        return False
    try:
        last_date = date.fromisoformat(str(last)[:10])
    except ValueError:
        return False
    return (today - last_date).days <= _DB_HISTORY_FRESHNESS_DAYS


async def get_history(symbol: str, months: int = 12) -> list[dict[str, Any]]:
    from services.ingest.repository import (
        OhlcvBar,
        read_ohlcv_range_autosession,
        upsert_ohlcv_bars_autosession,
    )

    key = key_history("tw", symbol, "1d")
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    today = date.today()
    start = today - timedelta(days=months * 30)

    # ── Tier 2: Postgres archive ────────────────────────────────
    db_bars = await read_ohlcv_range_autosession("TW", symbol, start, today)
    if _db_bars_are_fresh(db_bars, today):
        await cache_set(key, json.dumps(db_bars), TTL_HISTORY)
        return db_bars

    # ── Tier 3: upstream waterfall (TWSE month-by-month → FinMind) ──
    bars: list[dict] = []
    try:
        for i in range(months):
            d = today.replace(day=1) - timedelta(days=i * 30)
            month_bars = await twse.get_daily_ohlcv(symbol, d)
            bars = month_bars + bars
    except Exception:
        bars = []

    upstream_source = "twse"
    if not bars:
        try:
            bars = await finmind.get_daily_ohlcv(symbol, start.isoformat())
            upstream_source = "finmind"
        except Exception:
            pass

    if bars:
        await cache_set(key, json.dumps(bars), TTL_HISTORY)
        # Best-effort write-back into the archive so subsequent cache
        # misses serve from DB and reduce upstream load.
        ohlcv_bars = [
            b for b in (
                OhlcvBar.from_connector_row("TW", symbol, upstream_source, r)
                for r in bars
            )
            if b is not None
        ]
        await upsert_ohlcv_bars_autosession(ohlcv_bars)
        return bars

    # ── Tier 4: stale DB beats nothing during full upstream outage ──
    if db_bars:
        log.info("tw.history.served_stale_db",
                 extra={"symbol": symbol, "rows": len(db_bars)})
        return db_bars

    return []


# ── Institutional investors ───────────────────────────────────────

async def get_institutional(symbol: str, days: int = 30) -> list[dict[str, Any]]:
    key = key_institutional(symbol)
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    result: list[dict] = []

    # TWSE returns all stocks for one day; we'd need to call per day
    # so default to FinMind which returns per-symbol range
    start = (date.today() - timedelta(days=days)).isoformat()
    try:
        result = await finmind.get_institutional(symbol, start)
    except Exception:
        pass

    if not result:
        # TWSE fallback: today only
        try:
            all_rows = await twse.get_institutional()
            result = [r for r in all_rows if r.get("symbol") == symbol]
        except Exception:
            pass

    if result:
        await cache_set(key, json.dumps(result), TTL_INSTITUTIONAL)
    return result


# ── Margin balance ────────────────────────────────────────────────

async def get_margin(symbol: str, days: int = 30) -> list[dict[str, Any]]:
    key = key_margin(symbol)
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    start = (date.today() - timedelta(days=days)).isoformat()
    result: list[dict] = []
    try:
        result = await finmind.get_margin(symbol, start)
    except Exception:
        pass

    if not result:
        try:
            all_rows = await twse.get_margin()
            result = [r for r in all_rows if r.get("symbol") == symbol]
        except Exception:
            pass

    if result:
        await cache_set(key, json.dumps(result), TTL_MARGIN)
    return result


# ── Monthly revenue (月營收) ──────────────────────────────────────

async def get_revenue(symbol: str, months: int = 12) -> list[dict[str, Any]]:
    key = key_revenue(symbol)
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    start = _start_date(months)
    result: list[dict] = []
    try:
        result = await finmind.get_monthly_revenue(symbol, start)
    except Exception:
        pass

    if not result:
        # MOPS fallback: current month only
        try:
            today = date.today()
            result = await mops.get_monthly_revenue(symbol, today.year, today.month)
        except Exception:
            pass

    if result:
        await cache_set(key, json.dumps(result), TTL_REVENUE)
    return result


# ── Financials ────────────────────────────────────────────────────

async def get_fundamentals(symbol: str) -> dict[str, Any]:
    """PE, PB, dividend yield from TWSE BWIBBU_d + exchange info.

    Read tier: Redis → DB (within 7 days) → TWSE upstream → DB (any age).
    The 7-day window matches BWIBBU's typical refresh cadence — older
    rows are still better than blank state during a TWSE outage.
    """
    from services.ingest.repository import (
        FundamentalsSnapshotRow,
        read_latest_fundamentals_autosession,
        upsert_fundamentals_snapshots_autosession,
    )

    key = f"tw:fundamentals:{symbol}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    base = {
        "symbol": symbol,
        "market": "TW",
        "exchange": get_exchange(symbol),
    }

    # ── Tier 2: recent DB snapshot ─────────────────────────────────
    db_snap = await read_latest_fundamentals_autosession(
        "TW", symbol, max_age_days=7,
    )
    if db_snap is not None:
        result = {**base, **{
            "pe_ratio":       db_snap.get("pe_ratio"),
            "pb_ratio":       db_snap.get("pb_ratio"),
            "dividend_yield": db_snap.get("dividend_yield"),
            "fetched_at":     db_snap.get("as_of"),
            "data_source":    db_snap.get("data_source") or "db",
        }}
        await cache_set(key, json.dumps(result), TTL_FUNDAMENTALS)
        return result

    # ── Tier 3: TWSE upstream ──────────────────────────────────────
    have_ratios = False
    try:
        ratios = await twse.get_valuation_ratios(symbol)
        if ratios:
            base.update(ratios)
            have_ratios = True
    except Exception:
        pass

    if have_ratios:
        await cache_set(key, json.dumps(base), TTL_FUNDAMENTALS)
        # Best-effort write-back so the next request serves from DB.
        await upsert_fundamentals_snapshots_autosession([
            FundamentalsSnapshotRow(
                market="TW", symbol=symbol, as_of=date.today(),
                pe_ratio=base.get("pe_ratio"),
                pb_ratio=base.get("pb_ratio"),
                dividend_yield=base.get("dividend_yield"),
                eps=None, revenue=None, payload=None,
                source="twse",
            )
        ])
        return base

    # ── Tier 4: stale DB beats nothing during a TWSE outage ────────
    stale = await read_latest_fundamentals_autosession(
        "TW", symbol, max_age_days=365,
    )
    if stale is not None:
        log.info("tw.fundamentals.served_stale_db", extra={"symbol": symbol})
        return {**base, **{
            "pe_ratio":       stale.get("pe_ratio"),
            "pb_ratio":       stale.get("pb_ratio"),
            "dividend_yield": stale.get("dividend_yield"),
            "fetched_at":     stale.get("as_of"),
            "data_source":    "db_stale",
        }}

    return base


async def get_financials(symbol: str) -> list[dict[str, Any]]:
    key = f"tw:financials:{symbol}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)
    result = await finmind.get_financials(symbol)
    await cache_set(key, json.dumps(result), TTL_FUNDAMENTALS)
    return result


# ── Financial health (財務體質) ───────────────────────────────────
#
# Pivots FinMind rows ({date, type, value}) into per-period dicts, then
# derives margin / leverage / liquidity ratios and assigns red/yellow/green
# lights for the four StatementDog-style categories.

# FinMind type-field aliases — different report formats use different names.
_INCOME_ALIASES = {
    "revenue":         ("Revenue", "OperatingRevenue", "NetSales", "TotalRevenue"),
    "gross_profit":    ("GrossProfit", "GrossProfitFromOperatingActivities"),
    "operating_income": ("OperatingIncome", "OperatingProfit", "IncomeFromOperations"),
    "net_income":      (
        "NetIncome", "NetIncomeAttributableToOwnersOfParent",
        "IncomeAfterTax", "ProfitAfterTax",
    ),
    "eps":             ("EPS", "BasicEPS", "EarningsPerShare"),
}

_BALANCE_ALIASES = {
    "total_assets":         ("TotalAssets", "Assets"),
    "total_liabilities":    ("TotalLiabilities", "Liabilities"),
    "total_equity":         (
        "Equity", "TotalEquity", "EquityAttributableToOwnersOfParent",
    ),
    "current_assets":       ("CurrentAssets",),
    "current_liabilities":  ("CurrentLiabilities",),
}

_CASHFLOW_ALIASES = {
    "operating_cf":  (
        "CashFlowsFromOperatingActivities",
        "NetCashProvidedByOperatingActivities",
    ),
    "investing_cf":  (
        "CashFlowsFromInvestingActivities",
        "NetCashUsedInInvestingActivities",
    ),
    "capex":         (
        "AcquisitionOfPropertyPlantAndEquipment",
        "PurchaseOfPropertyPlantAndEquipment",
    ),
}


def _pivot_by_period(rows: list[dict]) -> dict[str, dict[str, float]]:
    """{date: {type: value}}. Last-write-wins for duplicate (date,type)."""
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        d = r.get("date")
        t = r.get("type")
        v = r.get("value")
        if not d or not t or v is None:
            continue
        try:
            out.setdefault(d, {})[t] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _pick(period: dict[str, float], names: tuple[str, ...]) -> float | None:
    for n in names:
        if n in period and period[n] is not None:
            return period[n]
    return None


def _safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _light(value: float | None, *, green: float, yellow: float, higher_better: bool = True) -> str:
    """Map a metric to red/yellow/green. Thresholds in original units (eg 15 = 15%)."""
    if value is None:
        return "gray"
    if higher_better:
        if value >= green:
            return "green"
        if value >= yellow:
            return "yellow"
        return "red"
    if value <= green:
        return "green"
    if value <= yellow:
        return "yellow"
    return "red"


async def get_health(symbol: str, periods: int = 8) -> dict[str, Any]:
    """
    Returns a structured StatementDog-style financial-health snapshot:

      {
        "symbol": ...,
        "periods":  [{"date", revenue, gross_margin, operating_margin,
                      net_margin, debt_ratio, current_ratio, eps,
                      operating_cf, free_cf}, ...],   # newest last
        "summary":  {"latest_roe", "latest_debt_ratio",
                     "revenue_yoy", "operating_cf_positive_streak"},
        "lights":   {"profitability", "safety", "growth", "cash_flow"}
      }
    """
    key = f"tw:health:{symbol}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    try:
        income_rows = await finmind.get_financials(symbol)
    except Exception:
        income_rows = []
    try:
        bs_rows = await finmind.get_balance_sheet(symbol)
    except Exception:
        bs_rows = []
    try:
        cf_rows = await finmind.get_cash_flow(symbol)
    except Exception:
        cf_rows = []

    income = _pivot_by_period(income_rows)
    bs     = _pivot_by_period(bs_rows)
    cf     = _pivot_by_period(cf_rows)

    all_dates = sorted(set(income) | set(bs) | set(cf))[-periods:]

    out_periods: list[dict[str, Any]] = []
    for d in all_dates:
        ip = income.get(d, {})
        bp = bs.get(d, {})
        cp = cf.get(d, {})

        revenue          = _pick(ip, _INCOME_ALIASES["revenue"])
        gross_profit     = _pick(ip, _INCOME_ALIASES["gross_profit"])
        operating_income = _pick(ip, _INCOME_ALIASES["operating_income"])
        net_income       = _pick(ip, _INCOME_ALIASES["net_income"])
        eps              = _pick(ip, _INCOME_ALIASES["eps"])

        total_assets        = _pick(bp, _BALANCE_ALIASES["total_assets"])
        total_liabilities   = _pick(bp, _BALANCE_ALIASES["total_liabilities"])
        total_equity        = _pick(bp, _BALANCE_ALIASES["total_equity"])
        current_assets      = _pick(bp, _BALANCE_ALIASES["current_assets"])
        current_liabilities = _pick(bp, _BALANCE_ALIASES["current_liabilities"])

        operating_cf = _pick(cp, _CASHFLOW_ALIASES["operating_cf"])
        capex        = _pick(cp, _CASHFLOW_ALIASES["capex"])

        gross_margin     = _safe_div(gross_profit, revenue)
        operating_margin = _safe_div(operating_income, revenue)
        net_margin       = _safe_div(net_income, revenue)
        debt_ratio       = _safe_div(total_liabilities, total_assets)
        current_ratio    = _safe_div(current_assets, current_liabilities)
        free_cf          = (operating_cf + capex) if (operating_cf is not None and capex is not None) else None

        out_periods.append({
            "date":             d,
            "revenue":          revenue,
            "net_income":       net_income,
            "eps":              eps,
            "gross_margin":     round(gross_margin * 100, 2) if gross_margin is not None else None,
            "operating_margin": round(operating_margin * 100, 2) if operating_margin is not None else None,
            "net_margin":       round(net_margin * 100, 2) if net_margin is not None else None,
            "debt_ratio":       round(debt_ratio * 100, 2) if debt_ratio is not None else None,
            "current_ratio":    round(current_ratio, 2) if current_ratio is not None else None,
            "operating_cf":     operating_cf,
            "free_cf":          free_cf,
            "total_equity":     total_equity,
        })

    # ── Summary metrics ─────────────────────────────────────────
    latest = out_periods[-1] if out_periods else {}

    # ROE on a TTM basis: sum of last 4 quarters' net income / latest equity.
    ttm_net_income: float | None = None
    last_4 = [p for p in out_periods[-4:] if p.get("net_income") is not None]
    if len(last_4) >= 1:
        ttm_net_income = sum(p["net_income"] for p in last_4)
    latest_equity = latest.get("total_equity")
    latest_roe = (
        round(ttm_net_income / latest_equity * 100, 2)
        if (ttm_net_income is not None and latest_equity)
        else None
    )

    # Operating cash-flow positive streak in last 4 periods.
    cf_streak = sum(
        1 for p in out_periods[-4:]
        if p.get("operating_cf") is not None and p["operating_cf"] > 0
    )

    # Revenue YoY: pull latest from the monthly_revenue series (already
    # YoY-computed). Avoids re-deriving from quarterly snapshots.
    revenue_yoy: float | None = None
    try:
        rev_rows = await get_revenue(symbol, months=3)
        if rev_rows:
            revenue_yoy = rev_rows[-1].get("revenue_yoy")
    except Exception:
        pass

    summary = {
        "latest_roe":       latest_roe,
        "latest_debt_ratio": latest.get("debt_ratio"),
        "latest_gross_margin": latest.get("gross_margin"),
        "latest_net_margin": latest.get("net_margin"),
        "revenue_yoy":      revenue_yoy,
        "cf_positive_streak_4q": cf_streak,
    }

    # ── Traffic-light scoring ───────────────────────────────────
    lights = {
        "profitability": _light(latest_roe, green=15, yellow=5, higher_better=True),
        "safety":        _light(latest.get("debt_ratio"), green=50, yellow=70, higher_better=False),
        "growth":        _light(revenue_yoy, green=10, yellow=0, higher_better=True),
        "cash_flow":     ("green" if cf_streak >= 4 else
                          "yellow" if cf_streak >= 2 else
                          "red" if out_periods else "gray"),
    }

    result = {
        "symbol":  symbol,
        "market":  "TW",
        "periods": out_periods,
        "summary": summary,
        "lights":  lights,
    }
    await cache_set(key, json.dumps(result), TTL_FUNDAMENTALS)
    return result


# ── Screener ──────────────────────────────────────────────────────

async def _get_etf_yields_cached() -> dict[str, float]:
    """TTM dividend yield for every TW ETF; populated by the daily
    refresh_tw_etf_yields scheduler job. Falls back to a 30-day
    last-known-good copy so the screener keeps filtering ETFs through
    transient FinMind quota / TWSE outages instead of silently dropping
    every high-yield ETF from results."""
    for key in ("tw:etf_yields_all", "tw:etf_yields_all:last_known"):
        cached = await cache_get(key)
        if not cached:
            continue
        try:
            return json.loads(cached)
        except (TypeError, ValueError):
            continue
    return {}


async def _get_all_valuations_cached() -> dict[str, dict[str, float | None]]:
    """
    Bulk PE/PB/dividend_yield for every TWSE-listed security.

    BWIBBU_ALL covers individual stocks only — it returns nothing for ETFs.
    We merge the daily-refreshed ETF yield map on top so the screener's
    `min_dividend_yield` filter applies uniformly across stocks and ETFs.
    """
    key = "tw:valuations:all"
    cached = await cache_get(key)
    if cached:
        data = json.loads(cached)
    else:
        try:
            data = await twse.get_all_valuation_ratios()
        except Exception:
            data = {}
        if data:
            await cache_set(key, json.dumps(data), TTL_FUNDAMENTALS)

    etf_yields = await _get_etf_yields_cached()
    if etf_yields:
        for sym, y in etf_yields.items():
            entry = data.setdefault(sym, {"pe_ratio": None, "pb_ratio": None, "dividend_yield": None})
            entry["dividend_yield"] = y

    return data


def _passes_fundamental_filters(
    v: dict[str, float | None],
    *,
    min_pe: float | None,
    max_pe: float | None,
    min_pb: float | None,
    max_pb: float | None,
    min_dividend_yield: float | None,
) -> bool:
    pe = v.get("pe_ratio")
    pb = v.get("pb_ratio")
    dy = v.get("dividend_yield")
    if min_pe is not None and (pe is None or pe < min_pe):
        return False
    if max_pe is not None and (pe is None or pe > max_pe):
        return False
    if min_pb is not None and (pb is None or pb < min_pb):
        return False
    if max_pb is not None and (pb is None or pb > max_pb):
        return False
    if min_dividend_yield is not None and (dy is None or dy < min_dividend_yield):
        return False
    return True


async def get_screener(
    exchange: str | None = None,    # "TWSE" | "TPEx" | None (both)
    min_volume: int | None = None,
    min_pe: float | None = None,
    max_pe: float | None = None,
    min_pb: float | None = None,
    max_pb: float | None = None,
    min_dividend_yield: float | None = None,
    include_etf: bool = True,
    etf_only: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    key = (
        f"tw:screener:{exchange}:{min_volume}:"
        f"{min_pe}:{max_pe}:{min_pb}:{max_pb}:{min_dividend_yield}:"
        f"{include_etf}:{etf_only}:{limit}"
    )
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    try:
        all_stocks = await twse.get_all_twse_symbols()
    except Exception as exc:
        log.warning("tw.screener.twse_symbols_failed", extra={"error": str(exc)})
        all_stocks = []

    # Sort by volume desc so the most-traded names surface first. The TWSE
    # endpoint orders by symbol ascending, which means the ~250 ETFs (00xxx)
    # bury every regular stock past the limit cap.
    all_stocks.sort(
        key=lambda s: twse._tw_int(s.get("成交股數") or s.get("TradeVolume", "0")),
        reverse=True,
    )

    needs_valuations = any(
        v is not None for v in (min_pe, max_pe, min_pb, max_pb, min_dividend_yield)
    )
    valuations = await _get_all_valuations_cached() if needs_valuations else {}

    result = []
    for s in all_stocks:
        code = (s.get("Code") or s.get("證券代號") or "").strip()
        if not code:
            continue
        symbol_is_etf = is_etf(code)
        if etf_only and not symbol_is_etf:
            continue
        if not include_etf and symbol_is_etf:
            continue
        vol = twse._tw_int(s.get("成交股數") or s.get("TradeVolume", "0"))
        if min_volume and vol < min_volume:
            continue
        exch = _exchange_map.get(code, "TWSE")
        if exchange and exch != exchange:
            continue

        v = valuations.get(code, {}) if valuations else {}
        if needs_valuations and not _passes_fundamental_filters(
            v,
            min_pe=min_pe, max_pe=max_pe,
            min_pb=min_pb, max_pb=max_pb,
            min_dividend_yield=min_dividend_yield,
        ):
            continue

        price = twse._tw_num(s.get("收盤價") or s.get("ClosingPrice"))
        change = twse._tw_num(s.get("漲跌價差") or s.get("Change"))
        change_pct = round(change / (price - change) * 100, 4) if (
            change is not None and price is not None and (price - change)
        ) else None

        result.append({
            "symbol":         code,
            "market":         "TW",
            "exchange":       exch,
            "name_zh":        s.get("Name") or s.get("證券名稱", ""),
            "price":          price,
            "change":         change,
            "change_pct":     change_pct,
            "volume":         vol,
            "pe_ratio":       v.get("pe_ratio") if v else None,
            "pb_ratio":       v.get("pb_ratio") if v else None,
            "dividend_yield": v.get("dividend_yield") if v else None,
            "data_source":    "twse",
        })
        if len(result) >= limit:
            break

    # Don't cache an empty screener for 10 min — mirrors the US service so
    # a transient TWSE OpenAPI failure doesn't lock the page into "no
    # results" until the next background refresh.
    if result:
        await cache_set(key, json.dumps(result), TTL_SCREENER)
    else:
        log.warning("tw.screener.empty_result")
    return result


# ── TAIEX index ───────────────────────────────────────────────────

async def get_index() -> dict[str, Any]:
    key = "tw:index:taiex"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)
    result = await twse.get_taiex()
    await cache_set(key, json.dumps(result), TTL_QUOTE)
    return result


# ── News ──────────────────────────────────────────────────────────

TTL_NEWS = 5 * 60


async def _google_news_rss(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """
    Hit Google News RSS for TW-localized headlines. RSS XML is plain text,
    no API key required. Each <item> has title / link / pubDate / source.
    Links are Google News redirect URLs that resolve to the original article
    when clicked.
    """
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    import httpx

    url = "https://news.google.com/rss/search"
    params = {
        "q":    query,
        "hl":   "zh-TW",
        "gl":   "TW",
        "ceid": "TW:zh-Hant",
    }
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        xml_text = r.text

    root = ET.fromstring(xml_text)
    items: list[dict[str, Any]] = []
    for el in root.findall(".//item")[:limit]:
        title = (el.findtext("title") or "").strip()
        link = (el.findtext("link") or "").strip()
        pub_date_raw = el.findtext("pubDate") or ""
        source_el = el.find("source")
        publisher = source_el.text.strip() if (source_el is not None and source_el.text) else ""
        try:
            published_at = parsedate_to_datetime(pub_date_raw).isoformat()
        except (TypeError, ValueError):
            published_at = pub_date_raw
        if not title or not link:
            continue
        items.append({
            "title":        title,
            "publisher":    publisher,
            "link":         link,
            "published_at": published_at,
            "thumbnail":    None,
        })
    return items


async def _yfinance_news_fallback(symbol: str, limit: int) -> list[dict[str, Any]]:
    """Legacy yfinance path. Often returns [] for TW symbols."""
    import asyncio
    from datetime import datetime
    loop = asyncio.get_running_loop()

    def _fetch():
        import yfinance as yf
        t = yf.Ticker(f"{symbol}.TW")
        raw = t.news or []
        items = []
        for n in raw[:limit]:
            thumbnail = None
            if t_data := n.get("thumbnail"):
                resolutions = t_data.get("resolutions", [])
                thumbnail = resolutions[0].get("url") if resolutions else None
            items.append({
                "title":        n.get("title", ""),
                "publisher":    n.get("publisher", ""),
                "link":         n.get("link", ""),
                "published_at": datetime.fromtimestamp(
                    n.get("providerPublishTime", 0), tz=UTC
                ).isoformat(),
                "thumbnail":    thumbnail,
            })
        return items

    return await loop.run_in_executor(None, _fetch)


async def get_news(symbol: str, limit: int = 10) -> list[dict[str, Any]]:
    """
    TW news read tier:
        Redis cache  →  DB (FinMind ingest archive)  →
        Google News RSS (live scrape)  →  yfinance fallback

    The DB tier serves recent FinMind articles populated by the hourly
    `ingest_news_tw` cron — no upstream call needed for cache misses.
    Google News RSS stays as the live scrape fallback because FinMind's
    coverage tags articles by `stock_id` so per-symbol matching may
    miss broad-market coverage.
    """
    from services.ingest.repository import read_recent_news_autosession

    key = f"tw:news:{symbol.upper()}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    # ── Tier 2: Postgres archive ────────────────────────────────
    db_items = await read_recent_news_autosession(
        "TW", symbol=symbol.upper(), limit=limit, max_age_days=14,
    )
    if db_items:
        await cache_set(key, json.dumps(db_items), TTL_NEWS)
        return db_items

    # ── Tier 3: live Google News RSS ────────────────────────────
    name = ""
    try:
        q_cached = await cache_get(key_quote("tw", symbol))
        if q_cached:
            name = json.loads(q_cached).get("name_zh", "") or ""
    except Exception:
        pass

    query = f"{symbol} {name}".strip() if name else f"{symbol} 台股"

    items: list[dict[str, Any]] = []
    try:
        items = await _google_news_rss(query, limit=limit)
    except Exception:
        items = []

    if not items:
        try:
            items = await _yfinance_news_fallback(symbol, limit)
        except Exception:
            items = []

    if items:
        await cache_set(key, json.dumps(items), TTL_NEWS)
    return items


# ── Valuation band (本益比 / 股價淨值比 河流圖) ──────────────────
#
# Derived on-demand from existing inputs — daily price history + quarterly
# EPS and total equity from FinMind. No daily snapshot table; the result
# is cached 24h. This trades a slightly stale cache (refreshes once a
# day) for zero schema/migration cost.

TTL_VALUATION_BAND = 24 * 3600


def _ttm_eps_at(period_end: str, eps_history: list[tuple[str, float]]) -> float | None:
    """Sum EPS over the last ≤4 reported quarters with period-end ≤ given date."""
    last_4 = [v for d, v in eps_history if d <= period_end][-4:]
    return sum(last_4) if last_4 else None


def _bvps_at(period_end: str,
             equity_history: list[tuple[str, float]],
             shares_history: list[tuple[str, float]]) -> float | None:
    """Latest equity / latest shares-outstanding estimate ≤ given date."""
    eq = next((v for d, v in reversed(equity_history) if d <= period_end), None)
    sh = next((v for d, v in reversed(shares_history) if d <= period_end), None)
    if eq is None or sh is None or sh <= 0:
        return None
    return eq / sh


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "std": None, "min": None, "max": None,
                "p10": None, "p25": None, "p50": None, "p75": None, "p90": None}
    sorted_v = sorted(values)
    n = len(sorted_v)
    mean = sum(sorted_v) / n
    var = sum((v - mean) ** 2 for v in sorted_v) / n
    std = var ** 0.5

    def _pct(p: float) -> float:
        if n == 1:
            return sorted_v[0]
        rank = p * (n - 1)
        lo = int(rank)
        frac = rank - lo
        hi = min(lo + 1, n - 1)
        return sorted_v[lo] * (1 - frac) + sorted_v[hi] * frac

    return {
        "mean": round(mean, 3),
        "std":  round(std, 3),
        "min":  round(sorted_v[0], 3),
        "max":  round(sorted_v[-1], 3),
        "p10":  round(_pct(0.10), 3),
        "p25":  round(_pct(0.25), 3),
        "p50":  round(_pct(0.50), 3),
        "p75":  round(_pct(0.75), 3),
        "p90":  round(_pct(0.90), 3),
    }


async def get_valuation_band(
    symbol: str,
    metric: str = "pe",       # "pe" | "pb"
    years: int = 5,
) -> dict[str, Any]:
    """
    Returns a daily PE or PB time series plus mean/std/percentile bands.
    PE  = close / TTM EPS  (negative or zero EPS → series gap)
    PB  = close / BVPS, where BVPS = total_equity / (NetIncome/EPS)
    """
    if metric not in ("pe", "pb"):
        raise ValueError(f"metric must be 'pe' or 'pb', got {metric!r}")

    key = f"tw:valuation_band:{symbol}:{metric}:{years}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    # ── Inputs ──────────────────────────────────────────────────
    bars = await get_history(symbol, months=years * 12)

    income_rows  = []
    balance_rows = []
    try:
        income_rows = await finmind.get_financials(symbol)
    except Exception:
        pass
    if metric == "pb":
        try:
            balance_rows = await finmind.get_balance_sheet(symbol)
        except Exception:
            pass

    income = _pivot_by_period(income_rows)
    bs     = _pivot_by_period(balance_rows)

    # Per-quarter EPS series (sorted ascending by date).
    eps_history: list[tuple[str, float]] = sorted(
        (d, _pick(p, _INCOME_ALIASES["eps"]))
        for d, p in income.items()
    )
    eps_history = [(d, v) for d, v in eps_history if v is not None]

    # Shares-outstanding series — derived as NetIncome / EPS where both
    # are reported. Unit ends up as "shares" if both are in absolute units;
    # since we only divide later by equity in the same unit system, the
    # per-share metric is consistent regardless of whether values are
    # stored in 千元 or 元 (units cancel as long as they match across rows).
    shares_history: list[tuple[str, float]] = []
    if metric == "pb":
        for d, p in sorted(income.items()):
            ni  = _pick(p, _INCOME_ALIASES["net_income"])
            eps = _pick(p, _INCOME_ALIASES["eps"])
            if ni is None or eps is None or eps == 0:
                continue
            shares_history.append((d, ni / eps))

    equity_history: list[tuple[str, float]] = sorted(
        (d, _pick(p, _BALANCE_ALIASES["total_equity"]))
        for d, p in bs.items()
    )
    equity_history = [(d, v) for d, v in equity_history if v is not None]

    # ── Build daily series ──────────────────────────────────────
    series: list[dict[str, Any]] = []
    for bar in bars:
        d = bar.get("time")
        close = bar.get("close")
        if not d or close is None:
            continue
        if metric == "pe":
            eps_ttm = _ttm_eps_at(d, eps_history)
            value = (close / eps_ttm) if (eps_ttm is not None and eps_ttm > 0) else None
        else:
            bvps = _bvps_at(d, equity_history, shares_history)
            value = (close / bvps) if (bvps is not None and bvps > 0) else None
        series.append({"date": d, "value": round(value, 3) if value is not None else None})

    # ── Stats over the non-null portion ─────────────────────────
    values = [p["value"] for p in series if p["value"] is not None]
    stats = _stats(values)
    current = next((p["value"] for p in reversed(series) if p["value"] is not None), None)
    current_z = (
        round((current - stats["mean"]) / stats["std"], 3)
        if (current is not None and stats["std"] and stats["std"] > 0)
        else None
    )

    result = {
        "symbol":  symbol,
        "metric":  metric,
        "series":  series,
        "stats":   {**stats, "current": current, "current_z": current_z},
    }
    if values:
        await cache_set(key, json.dumps(result), TTL_VALUATION_BAND)
    return result


# ── Dividends + ETF holdings ──────────────────────────────────────

TTL_DIVIDENDS    = 24 * 3600
TTL_ETF_HOLDINGS = 24 * 3600


def _normalize_dividend(r: dict) -> dict[str, Any]:
    """
    FinMind TaiwanStockDividend rows vary year-over-year. Common keys:
      date / CashEarningsDistribution / StockEarningsDistribution
      / CashStatutorySurplus / StockStatutorySurplus
      / CashExDividendTradingDate / StockExDividendTradingDate
    Collapse into a single (date, cash_dividend, stock_dividend) row.
    """
    cash = (
        (r.get("CashEarningsDistribution") or 0)
        + (r.get("CashStatutorySurplus") or 0)
    )
    stock = (
        (r.get("StockEarningsDistribution") or 0)
        + (r.get("StockStatutorySurplus") or 0)
    )
    return {
        "date":            r.get("date") or r.get("CashExDividendTradingDate"),
        "ex_date":         r.get("CashExDividendTradingDate") or r.get("StockExDividendTradingDate"),
        "cash_dividend":   round(float(cash), 4) if cash else 0.0,
        "stock_dividend":  round(float(stock), 4) if stock else 0.0,
    }


async def get_dividends(symbol: str) -> list[dict[str, Any]]:
    """Cash + stock dividend history, normalized and oldest-first."""
    key = f"tw:dividends:{symbol}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    rows: list[dict] = []
    try:
        rows = await finmind.get_dividends(symbol)
    except Exception:
        rows = []

    out = [_normalize_dividend(r) for r in rows]
    out = [r for r in out if r["date"] and (r["cash_dividend"] or r["stock_dividend"])]
    out.sort(key=lambda r: r["date"])

    await cache_set(key, json.dumps(out), TTL_DIVIDENDS)
    return out


async def get_etf_holdings(symbol: str) -> dict[str, Any]:
    """
    Top constituents with weights, latest snapshot only. Returns
    {"as_of": None, "holdings": []} if FinMind doesn't expose holdings
    for this ETF (free-tier restriction or not-an-ETF). Frontend
    renders an empty state in that case.
    """
    empty: dict[str, Any] = {"as_of": None, "holdings": []}
    if not is_etf(symbol):
        return empty

    key = f"tw:etf_holdings:{symbol}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    rows: list[dict] = []
    try:
        rows = await finmind.get_etf_holdings(symbol)
    except Exception:
        rows = []

    if not rows:
        return empty

    latest_date = max(r.get("date", "") for r in rows)
    snapshot = [r for r in rows if r.get("date") == latest_date]

    out: list[dict[str, Any]] = []
    for r in snapshot:
        # FinMind field names vary: stock_id / SecurityCode and
        # weight / Weight / shares_per. Try several.
        sym = r.get("stock_id") or r.get("SecurityCode") or r.get("symbol", "")
        weight = (
            r.get("weight")
            or r.get("Weight")
            or r.get("shares_per")
            or r.get("HoldingPct")
        )
        try:
            weight_f = float(weight) if weight is not None else None
        except (TypeError, ValueError):
            weight_f = None
        if not sym or weight_f is None:
            continue
        out.append({
            "symbol":   sym,
            "name_zh":  r.get("stock_name") or r.get("name_zh") or "",
            "weight":   round(weight_f, 4),
        })

    out.sort(key=lambda r: r["weight"], reverse=True)
    result = {"as_of": latest_date, "holdings": out}
    await cache_set(key, json.dumps(result), TTL_ETF_HOLDINGS)
    return result
