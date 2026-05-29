"""TW financial-health (財務體質) — pulled out of ``tw_market_service``
(PR-C of the long-running TW-service split).

Owns the StatementDog-style pivot + ratio + traffic-light pipeline that
backs ``get_health``:

  pivot      ``_pivot_by_period`` — {date: {type: value}} flatten with
             last-write-wins on duplicate (date, type)
  picker     ``_pick`` — alias-tolerant accessor across FinMind's
             multiple naming conventions
  ratios     ``_safe_div`` — None / 0-tolerant divide for the four
             margin / leverage / liquidity ratios
  scoring    ``_light`` — value → red / yellow / green / gray
  public     ``get_health`` — orchestrator

The three alias dicts at the top (`_INCOME_ALIASES`, `_BALANCE_ALIASES`,
`_CASHFLOW_ALIASES`) cover the rotating column names FinMind has shipped
across its financial-statement endpoints since 2019. Adding a new alias
= append to the tuple; the picker walks them in order.

``tw_market_service`` re-exports every name here so existing call sites
keep working — including the test suite at ``test_tw_health_service.py``
which directly invokes ``svc.get_health`` / ``svc._safe_div`` /
``svc._light``, and ``api/tw_market/router.py`` which is patched in
``test_tw_market_api.py`` via ``patch("services.tw_market_service.
get_health", ...)``.

``get_health`` calls back into ``tw_market_service.get_revenue`` for
the YoY headline. That's done as a lazy import to keep this module from
importing the rest of the service at module-load time — the same
pattern the prev_close chain uses for ``read_ohlcv_range_autosession``.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import data.tw.finmind_connector as finmind
from cache.cache_ttls import TTL_FUNDAMENTALS
from cache.redis_cache import cache_get, cache_set

log = logging.getLogger(__name__)


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
    # YoY-computed). Avoids re-deriving from quarterly snapshots. Lazy
    # import — get_revenue lives in tw_market_service and re-exporting
    # the other direction would create a load-time circular import.
    revenue_yoy: float | None = None
    try:
        from services.tw_market_service import get_revenue
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
