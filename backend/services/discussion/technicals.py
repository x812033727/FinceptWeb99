"""Pure technicals + financial-summary helpers for focus briefs.

Eight pure functions that turn raw OHLCV / institutional / revenue
rows into the typed summary dicts the per-symbol "focus brief"
prompt block surfaces. Originally lived inline in
``discussion_service.py`` next to the focus-brief builders; pulled
out so the brief builders only need to know the high-level shape
(``_compute_technicals(bars)`` → dict) instead of carrying RSI math
+ revenue-row slicing in the same file as the LLM orchestration.

  - ``_bar_close`` — defensive float-coerce of one OHLCV bar's close
    (FinMind / Stooq sometimes return strings; an unparseable value
    drops the bar instead of crashing the brief).
  - ``_ma`` — N-period simple moving average; None when window is
    larger than the available history.
  - ``_rsi`` — Wilder's RSI on the last N returns. None when bars
    are too thin so a fresh-listed name doesn't get a misleading 50.
  - ``_pct_change`` — start → end % change with zero / None guards.
  - ``_compute_technicals`` — top-level summariser: 20/60/120 MA,
    52-week high / low, distance-from-extremes, 5d / 20d / 60d perf,
    RSI-14. Returns None when fewer than 20 bars are available
    (signal too thin, persona is better off seeing "技術指標不足"
    than misleading numbers).
  - ``_summarize_revenue`` — last N months of monthly revenue rows
    flattened to ``[{month, revenue_yoy, revenue_mom}, ...]``.
  - ``_summarize_institutional`` — net foreign / SITC / dealer over
    the last 5 trading days. None when no rows.
  - ``_summarize_margin`` — most-recent margin / short balance.

All eight are pure (no DB / LLM / I/O), so unit tests hit them
directly. ``discussion_service`` re-exports the names + the two
window constants (``_FOCUS_BRIEF_REVENUE_MONTHS``,
``_FOCUS_BRIEF_CHIP_DAYS``) it still references from the focus
brief builder bodies.
"""
from __future__ import annotations

from typing import Any

# Window constants used by the summaries. The focus-brief builders
# (still in discussion_service) also reference these; they're
# re-exported there for back-compat.
_FOCUS_BRIEF_REVENUE_MONTHS = 6
_FOCUS_BRIEF_CHIP_DAYS = 5


def _bar_close(bar: dict[str, Any]) -> float | None:
    c = bar.get("close")
    try:
        return float(c) if c is not None else None
    except (TypeError, ValueError):
        return None


def _ma(closes: list[float], window: int) -> float | None:
    if len(closes) < window:
        return None
    return round(sum(closes[-window:]) / window, 4)


def _rsi(closes: list[float], window: int = 14) -> float | None:
    """Wilder's RSI on the last `window` returns. Falls through to None
    when there's not enough data — fresh-listed names that landed in
    the topic get a None instead of a misleading 50."""
    if len(closes) <= window:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas[-window:]]
    losses = [max(-d, 0.0) for d in deltas[-window:]]
    avg_gain = sum(gains) / window
    avg_loss = sum(losses) / window
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def _pct_change(start: float | None, end: float | None) -> float | None:
    if not start or not end or start == 0:
        return None
    return round((end - start) / start * 100.0, 2)


def _compute_technicals(bars: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Compute summary technicals from a daily-OHLCV history list.

    Returns None when fewer than 20 bars available — the moving
    averages would be too thin to carry signal and the persona is
    better off seeing "技術指標不足" than misleading numbers.
    """
    closes = [c for c in (_bar_close(b) for b in bars) if c is not None]
    if len(closes) < 20:
        return None
    last = closes[-1]
    high_52w = max(closes[-min(252, len(closes)):])
    low_52w = min(closes[-min(252, len(closes)):])
    return {
        "last_close":      round(last, 4),
        "ma20":            _ma(closes, 20),
        "ma60":            _ma(closes, 60),
        "ma120":           _ma(closes, 120),
        "high_52w":        round(high_52w, 4),
        "low_52w":         round(low_52w, 4),
        "dist_high_52w_pct": _pct_change(high_52w, last),
        "dist_low_52w_pct":  _pct_change(low_52w, last),
        "perf_5d_pct":     _pct_change(
            closes[-6] if len(closes) >= 6 else None, last,
        ),
        "perf_20d_pct":    _pct_change(
            closes[-21] if len(closes) >= 21 else None, last,
        ),
        "perf_60d_pct":    _pct_change(
            closes[-61] if len(closes) >= 61 else None, last,
        ),
        "rsi14":           _rsi(closes, 14),
    }


def _summarize_revenue(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Take the latest `_FOCUS_BRIEF_REVENUE_MONTHS` rows from a
    `tw_market_service.get_revenue` response, drop noise fields."""
    if not rows:
        return []
    tail = rows[-_FOCUS_BRIEF_REVENUE_MONTHS:]
    out: list[dict[str, Any]] = []
    for r in tail:
        out.append({
            "month":       (r.get("date") or "")[:7],
            "revenue_yoy": r.get("revenue_yoy"),
            "revenue_mom": r.get("revenue_mom"),
            "data_source": r.get("data_source", "unknown"),
            "as_of":       r.get("date"),
        })
    return out


def _summarize_institutional(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Sum 5-day net foreign / SITC / dealer over the rows. Returns
    None when nothing came back — caller drops the block."""
    if not rows:
        return None
    fini_net = sitc_net = dealer_net = 0
    days = 0
    for r in rows[-_FOCUS_BRIEF_CHIP_DAYS:]:
        fini_net += int(r.get("fini_buy") or 0) - int(r.get("fini_sell") or 0)
        sitc_net += int(r.get("sitc_buy") or 0) - int(r.get("sitc_sell") or 0)
        dealer_net += int(r.get("dealer_buy") or 0) - int(r.get("dealer_sell") or 0)
        days += 1
    if days == 0:
        return None
    return {
        "fini_net_5d":   fini_net,
        "sitc_net_5d":   sitc_net,
        "dealer_net_5d": dealer_net,
        "days":          days,
        "data_source":   rows[-1].get("data_source", "unknown"),
        "as_of":         rows[-1].get("date"),
    }


def _summarize_margin(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    latest = rows[-1]
    return {
        "as_of":           latest.get("date"),
        "margin_balance":  latest.get("margin_balance"),
        "short_balance":   latest.get("short_balance"),
        "data_source":     latest.get("data_source", "unknown"),
    }
