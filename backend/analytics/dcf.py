"""
DCF valuation model — pure NumPy, no I/O.

Inputs:
  fcf_history       list of annual free cash flow (most recent last)
  growth_rate_1     growth rate for years 1-5
  growth_rate_2     growth rate for years 6-10
  terminal_growth   perpetuity growth rate
  wacc              weighted average cost of capital
  shares            shares outstanding
  net_debt          total debt minus cash (can be negative)

Output dict:
  intrinsic_value   per share
  margin_of_safety  % discount to current price
  sensitivity       grid of intrinsic_value vs wacc × terminal_growth
  scenarios         bull / base / bear intrinsic values
"""
from typing import Any


def _discount_fcfs(fcfs: list[float], wacc: float) -> float:
    pv = 0.0
    for t, fcf in enumerate(fcfs, start=1):
        pv += fcf / (1 + wacc) ** t
    return pv


def _terminal_value(fcf_year10: float, terminal_growth: float, wacc: float, discount_years: int = 10) -> float:
    if wacc <= terminal_growth:
        return 0.0
    tv = fcf_year10 * (1 + terminal_growth) / (wacc - terminal_growth)
    return tv / (1 + wacc) ** discount_years


def _project_fcfs(base_fcf: float, g1: float, g2: float, years: int = 10) -> list[float]:
    fcfs = []
    fcf = base_fcf
    for t in range(1, years + 1):
        g = g1 if t <= 5 else g2
        fcf *= (1 + g)
        fcfs.append(fcf)
    return fcfs


def run_dcf(
    fcf_history: list[float],
    growth_rate_1: float,
    growth_rate_2: float,
    terminal_growth: float,
    wacc: float,
    shares: float,
    net_debt: float = 0.0,
    current_price: float | None = None,
) -> dict[str, Any]:
    if not fcf_history or shares <= 0:
        return {}

    base_fcf = fcf_history[-1]

    # ── Base scenario ─────────────────────────────────────────────
    projected = _project_fcfs(base_fcf, growth_rate_1, growth_rate_2)
    pv_fcf = _discount_fcfs(projected, wacc)
    pv_tv  = _terminal_value(projected[-1], terminal_growth, wacc)
    equity_value = pv_fcf + pv_tv - net_debt
    intrinsic_value = max(equity_value / shares, 0.0)

    margin_of_safety = None
    if current_price and current_price > 0:
        margin_of_safety = round((intrinsic_value - current_price) / current_price * 100, 2)

    # ── Sensitivity grid: WACC × terminal_growth ──────────────────
    wacc_range = [wacc - 0.02, wacc - 0.01, wacc, wacc + 0.01, wacc + 0.02]
    tg_range   = [terminal_growth - 0.01, terminal_growth, terminal_growth + 0.01]

    grid: list[list[float]] = []
    for w in wacc_range:
        row = []
        for tg in tg_range:
            p = _project_fcfs(base_fcf, growth_rate_1, growth_rate_2)
            pv = _discount_fcfs(p, w) + _terminal_value(p[-1], tg, w) - net_debt
            row.append(round(max(pv / shares, 0.0), 2))
        grid.append(row)

    # ── Bull / Base / Bear scenarios ──────────────────────────────
    scenarios: dict[str, float] = {}
    for name, (g1, g2, tg, w) in {
        "bull": (growth_rate_1 * 1.3, growth_rate_2 * 1.2, terminal_growth * 1.1, wacc * 0.95),
        "base": (growth_rate_1,       growth_rate_2,        terminal_growth,       wacc),
        "bear": (growth_rate_1 * 0.7, growth_rate_2 * 0.6, terminal_growth * 0.8, wacc * 1.05),
    }.items():
        p = _project_fcfs(base_fcf, g1, g2)
        pv = _discount_fcfs(p, w) + _terminal_value(p[-1], tg, w) - net_debt
        scenarios[name] = round(max(pv / shares, 0.0), 2)

    return {
        "intrinsic_value":    round(intrinsic_value, 2),
        "equity_value":       round(equity_value, 2),
        "pv_fcf":             round(pv_fcf, 2),
        "pv_terminal":        round(pv_tv, 2),
        "margin_of_safety":   margin_of_safety,
        "sensitivity": {
            "wacc":            [round(w, 4) for w in wacc_range],
            "terminal_growth": [round(tg, 4) for tg in tg_range],
            "values":          grid,
        },
        "scenarios": scenarios,
    }
