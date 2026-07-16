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

import logging
from typing import Any

import data.tw.finmind_connector as finmind
from cache.cache_ttls import TTL_FUNDAMENTALS
from cache.redis_cache import cache_get_json, cache_set_json, key_health_tw
from middleware.metrics import FINANCIAL_STATEMENT_ANALYSIS_TOTAL

log = logging.getLogger(__name__)


# FinMind type-field aliases — different report formats use different names.
_INCOME_ALIASES = {
    "revenue":         ("Revenue", "OperatingRevenue", "NetSales", "TotalRevenue"),
    "gross_profit":    ("GrossProfit", "GrossProfitFromOperatingActivities"),
    "operating_income": ("OperatingIncome", "OperatingProfit", "IncomeFromOperations"),
    "net_income":      (
        "NetIncome", "NetIncomeAttributableToOwnersOfParent",
        "IncomeAfterTax", "IncomeAfterTaxes", "ProfitAfterTax",
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
    "cash":                  (
        "CashAndCashEquivalents", "CashCashEquivalentsAndCurrentFinancialAssets",
    ),
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
        "PropertyAndPlantAndEquipment",
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


def _round_pct(value: float | None) -> float | None:
    return round(value * 100, 2) if value is not None else None


def _growth_pct(current: float | None, previous: float | None) -> float | None:
    """Percentage growth with an explicit zero / missing-data abstention."""
    ratio = _safe_div(
        current - previous if current is not None and previous is not None else None,
        abs(previous) if previous is not None else None,
    )
    return _round_pct(ratio)


def _complete_sum(periods: list[dict[str, Any]], key: str) -> float | None:
    """Sum only a complete four-quarter window; never annualise partial data."""
    if len(periods) != 4 or any(p.get(key) is None for p in periods):
        return None
    return sum(float(p[key]) for p in periods)


def _quarterize_cash_flow(
    periods: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    """Convert calendar-year YTD cash-flow facts into standalone quarters.

    Taiwan interim cash-flow statements are year-to-date. Q2/Q3/annual facts
    must therefore subtract the prior filing in the same year before TTM sums.
    If an earlier filing is missing, affected flow facts are removed so callers
    abstain instead of treating an unknown YTD value as one quarter.
    """
    target_names = {
        name
        for aliases in _CASHFLOW_ALIASES.values()
        for name in aliases
    }
    result: dict[str, dict[str, float]] = {}
    previous_date: str | None = None
    previous: dict[str, float] = {}
    for date_key in sorted(periods):
        current = dict(periods[date_key])
        try:
            year, month = int(date_key[:4]), int(date_key[5:7])
            previous_year = int(previous_date[:4]) if previous_date else None
        except (TypeError, ValueError):
            result[date_key] = current
            previous_date, previous = date_key, periods[date_key]
            continue
        if month > 3:
            for name in target_names & current.keys():
                if previous_year == year and name in previous:
                    current[name] -= previous[name]
                else:
                    current.pop(name, None)
        result[date_key] = current
        previous_date, previous = date_key, periods[date_key]
    return result


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
    key = key_health_tw(symbol, periods)
    cached = await cache_get_json(key)
    if cached is not None:
        cached_status = cached.get("quality", {}).get("status", "unavailable")
        FINANCIAL_STATEMENT_ANALYSIS_TOTAL.labels(outcome=cached_status).inc()
        return cached

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

    result = compute_health(
        symbol, income_rows, bs_rows, cf_rows,
        periods=periods, revenue_yoy=revenue_yoy,
    )
    FINANCIAL_STATEMENT_ANALYSIS_TOTAL.labels(
        outcome=result["quality"]["status"],
    ).inc()
    await cache_set_json(key, result, TTL_FUNDAMENTALS)
    return result


def compute_health(
    symbol: str,
    income_rows: list[dict],
    bs_rows: list[dict],
    cf_rows: list[dict],
    *,
    periods: int = 8,
    revenue_yoy: float | None = None,
) -> dict[str, Any]:
    """Pure statement-math half of `get_health` — no I/O, no cache.

    Split out so bulk callers can drive it from FinMind's market-wide
    statement datasets (one call per quarter for ~2000 companies) rather
    than fanning out three per-symbol fetches. `revenue_yoy` is injected
    because its source is the monthly-revenue archive, not the
    statements; bulk callers that don't need it pass None.
    """
    income = _pivot_by_period(income_rows)
    bs     = _pivot_by_period(bs_rows)
    cf     = _quarterize_cash_flow(_pivot_by_period(cf_rows))

    # Keep four extra quarters internally for same-quarter YoY and opening
    # balance-sheet averages, while returning only the caller's window.
    all_dates = sorted(set(income) | set(bs) | set(cf))[-max(periods + 4, 5):]

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
        # XBRL taxonomies disagree on whether acquisition capex is signed.
        # Treat either representation as an outflow so FCF remains comparable.
        free_cf          = (operating_cf - abs(capex)) if (operating_cf is not None and capex is not None) else None
        cash             = _pick(bp, _BALANCE_ALIASES["cash"])

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
            "total_assets":     total_assets,
            "total_liabilities": total_liabilities,
            "current_assets":   current_assets,
            "current_liabilities": current_liabilities,
            "cash":             cash,
            "capex":            capex,
            "revenue_yoy":      None,
            "net_income_yoy":   None,
            "eps_yoy":          None,
            "cash_conversion":  round(_safe_div(operating_cf, net_income), 3)
                                if _safe_div(operating_cf, net_income) is not None else None,
            "free_cf_margin":   _round_pct(_safe_div(free_cf, revenue)),
        })

    # Same-quarter YoY comparisons. Matching month/day is safer than blindly
    # taking i-4 when one statement has a missing period.
    by_month_day_year = {
        (p["date"][5:], int(p["date"][:4])): p
        for p in out_periods
        if len(p.get("date", "")) >= 10 and p["date"][:4].isdigit()
    }
    for p in out_periods:
        d = p.get("date", "")
        if len(d) < 10 or not d[:4].isdigit():
            continue
        prior = by_month_day_year.get((d[5:], int(d[:4]) - 1))
        if prior is None:
            continue
        p["revenue_yoy"] = _growth_pct(p.get("revenue"), prior.get("revenue"))
        p["net_income_yoy"] = _growth_pct(p.get("net_income"), prior.get("net_income"))
        p["eps_yoy"] = _growth_pct(p.get("eps"), prior.get("eps"))

    # ── Summary metrics ─────────────────────────────────────────
    latest = out_periods[-1] if out_periods else {}

    # TTM calculations require a complete four-quarter window. Partial windows
    # are returned as null rather than being silently annualised.
    last_4 = out_periods[-4:]
    ttm_revenue = _complete_sum(last_4, "revenue")
    ttm_net_income = _complete_sum(last_4, "net_income")
    ttm_operating_cf = _complete_sum(last_4, "operating_cf")
    ttm_free_cf = _complete_sum(last_4, "free_cf")
    latest_equity = latest.get("total_equity")
    latest_assets = latest.get("total_assets")
    opening = out_periods[-5] if len(out_periods) >= 5 else {}
    opening_equity = opening.get("total_equity")
    opening_assets = opening.get("total_assets")
    avg_equity = (
        (latest_equity + opening_equity) / 2
        if latest_equity is not None and opening_equity is not None
        else latest_equity
    )
    avg_assets = (
        (latest_assets + opening_assets) / 2
        if latest_assets is not None and opening_assets is not None
        else latest_assets
    )
    latest_roe = (
        _round_pct(_safe_div(ttm_net_income, avg_equity))
        if ttm_net_income is not None
        else None
    )
    latest_roa = _round_pct(_safe_div(ttm_net_income, avg_assets))
    ttm_net_margin = _round_pct(_safe_div(ttm_net_income, ttm_revenue))
    asset_turnover = _safe_div(ttm_revenue, avg_assets)
    equity_multiplier = _safe_div(avg_assets, avg_equity)
    dupont_roe = (
        _round_pct(ttm_net_margin / 100 * asset_turnover * equity_multiplier)
        if ttm_net_margin is not None
        and asset_turnover is not None
        and equity_multiplier is not None
        else None
    )
    cash_conversion_ttm = _safe_div(ttm_operating_cf, ttm_net_income)

    # Operating cash-flow positive streak in last 4 periods.
    cf_streak = sum(
        1 for p in out_periods[-4:]
        if p.get("operating_cf") is not None and p["operating_cf"] > 0
    )

    summary = {
        "latest_roe":       latest_roe,
        "latest_debt_ratio": latest.get("debt_ratio"),
        "latest_gross_margin": latest.get("gross_margin"),
        "latest_net_margin": latest.get("net_margin"),
        "revenue_yoy":      revenue_yoy,
        "cf_positive_streak_4q": cf_streak,
        "latest_roa": latest_roa,
        "ttm_revenue": ttm_revenue,
        "ttm_net_income": ttm_net_income,
        "ttm_operating_cf": ttm_operating_cf,
        "ttm_free_cf": ttm_free_cf,
        "ttm_net_margin": ttm_net_margin,
        "cash_conversion_ttm": round(cash_conversion_ttm, 3)
                               if cash_conversion_ttm is not None else None,
        "asset_turnover": round(asset_turnover, 3) if asset_turnover is not None else None,
        "equity_multiplier": round(equity_multiplier, 3) if equity_multiplier is not None else None,
        "dupont_roe": dupont_roe,
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

    statement_periods = {
        "income": len(income), "balance_sheet": len(bs), "cash_flow": len(cf),
    }
    latest_core = [
        latest.get(k) for k in (
            "revenue", "net_income", "gross_margin", "operating_margin",
            "total_assets", "total_liabilities", "total_equity",
            "current_assets", "current_liabilities", "operating_cf", "capex",
        )
    ]
    coverage_pct = round(sum(v is not None for v in latest_core) / len(latest_core) * 100, 1)
    quality_flags: list[str] = []
    if not income:
        quality_flags.append("missing_income_statement")
    if not bs:
        quality_flags.append("missing_balance_sheet")
    if not cf:
        quality_flags.append("missing_cash_flow")
    if coverage_pct < 80:
        quality_flags.append("incomplete_latest_period")
    if len(out_periods) < 5:
        quality_flags.append("limited_yoy_history")
    quality_status = (
        "unavailable" if not out_periods
        else "good" if coverage_pct >= 80 and all(statement_periods.values())
        else "degraded"
    )

    signals: list[dict[str, Any]] = []
    prior_year = out_periods[-5] if len(out_periods) >= 5 else None
    if prior_year is not None:
        margin_change = (
            latest["gross_margin"] - prior_year["gross_margin"]
            if latest.get("gross_margin") is not None and prior_year.get("gross_margin") is not None
            else None
        )
        debt_change = (
            latest["debt_ratio"] - prior_year["debt_ratio"]
            if latest.get("debt_ratio") is not None and prior_year.get("debt_ratio") is not None
            else None
        )
        if margin_change is not None and abs(margin_change) >= 2:
            signals.append({
                "code": "gross_margin_expanding" if margin_change > 0 else "gross_margin_contracting",
                "direction": "positive" if margin_change > 0 else "risk",
                "value": round(margin_change, 2), "unit": "percentage_points",
            })
        if debt_change is not None and abs(debt_change) >= 5:
            signals.append({
                "code": "leverage_improving" if debt_change < 0 else "leverage_rising",
                "direction": "positive" if debt_change < 0 else "risk",
                "value": round(debt_change, 2), "unit": "percentage_points",
            })
    if cash_conversion_ttm is not None and ttm_net_income is not None and ttm_net_income > 0:
        if cash_conversion_ttm < 0.8:
            signals.append({"code": "weak_cash_conversion", "direction": "risk",
                            "value": round(cash_conversion_ttm, 3), "unit": "ratio"})
        elif cash_conversion_ttm >= 1:
            signals.append({"code": "strong_cash_conversion", "direction": "positive",
                            "value": round(cash_conversion_ttm, 3), "unit": "ratio"})
    if ttm_free_cf is not None and ttm_free_cf < 0:
        signals.append({"code": "negative_ttm_free_cash_flow", "direction": "risk",
                        "value": ttm_free_cf, "unit": "reported_currency"})

    result = {
        "symbol":  symbol,
        "market":  "TW",
        "periods": out_periods[-periods:],
        "summary": summary,
        "lights":  lights,
        "signals": signals,
        "quality": {
            "status": quality_status,
            "flags": quality_flags,
            "sources": ["finmind"] if out_periods else [],
            "statement_periods": statement_periods,
            "latest_core_coverage_pct": coverage_pct,
        },
        "methodology": {
            "ttm": "sum of four complete standalone quarters; partial windows abstain",
            "cash_flow_periods": "calendar-year YTD facts are differenced into standalone quarters",
            "free_cash_flow": "operating cash flow minus absolute acquisition capex",
            "roe": "TTM net income divided by average opening/latest equity when available",
            "dupont": "TTM net margin × asset turnover × equity multiplier",
            "signals": "descriptive thresholds only; not investment advice",
        },
    }
    return result
