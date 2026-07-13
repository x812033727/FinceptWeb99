"""Macro context block (FRED-backed) for the discussion ctx.

Pulls 5 FRED series concurrently (Fed Funds rate, US 10Y yield,
10Y-2Y spread, USD index, TWD/USD), reduces each to its latest
value plus 1y / 3m delta, and returns the typed dict the persona
prompt block surfaces.

Two pieces:

  - ``_macro_summary_from_series`` (pure) — collapse a FRED
    ``[{date, value}]`` time series to ``{latest_date, latest_value,
    change_1y, change_3m}``. None when the series is empty.
  - ``_assemble_macro_block`` (async) — fan out via
    ``asyncio.gather`` against ``us_market_service.get_macro_indicator``
    (lazy-imported), label each, summarise. Empty / failing series
    degrade to ``summary=None`` so the personas can mention "macro
    data incomplete" instead of confidently citing a missing rate.

``as_of`` (PR #226): backtest mode. Forwarded into FRED's
``observation_end`` so each series excludes data points after the
backtest anchor.

discussion_service.py re-exports both names for back-compat with
``gather_market_context`` (still in the monolith) and the lazy import
in ``discussion/context/blocks/http.py``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

log = logging.getLogger(__name__)


_MACRO_SERIES = (
    ("fed_funds_rate", "Fed Funds Rate (%)"),
    ("10y_yield",      "US 10Y Treasury (%)"),
    ("10y_minus_2y",   "10Y-2Y Spread (%)"),
    ("usd_index",      "USD Index (DXY)"),
    ("twd_usd",        "TWD/USD"),
)


def _macro_summary_from_series(
    series: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Reduce a FRED `[{date, value}]` time series to:
        {latest_date, latest_value, change_1y, change_3m}.
    Returns None if the series came back empty."""
    if not series:
        return None
    points: list[tuple[str, float]] = []
    for p in series:
        try:
            v = float(p["value"])
        except (TypeError, ValueError, KeyError):
            continue
        d = p.get("date")
        if not d:
            continue
        points.append((d, v))
    if not points:
        return None
    points.sort(key=lambda p: p[0])
    latest_date, latest_value = points[-1]

    def _earlier_than(target_days: int) -> float | None:
        from datetime import date as _date
        from datetime import timedelta as _td
        try:
            ld = _date.fromisoformat(latest_date)
        except ValueError:
            return None
        target = ld - _td(days=target_days)
        # Find the closest point on or before `target`.
        best: float | None = None
        for d, v in points:
            try:
                dd = _date.fromisoformat(d)
            except ValueError:
                continue
            if dd <= target:
                best = v
        return best

    # Series-level change deltas — for rates we report absolute change
    # (basis points), for everything else relative %. Personas read
    # these to anchor "Fed cut 75bp YoY" vs "DXY +6% YoY".
    one_y_ago = _earlier_than(365)
    three_m_ago = _earlier_than(90)
    return {
        "latest_date":  latest_date,
        "latest_value": round(latest_value, 4),
        "change_1y":    None if one_y_ago is None
                        else round(latest_value - one_y_ago, 4),
        "change_3m":    None if three_m_ago is None
                        else round(latest_value - three_m_ago, 4),
    }


async def _assemble_macro_block(
    *, as_of: date | None = None,
) -> dict[str, Any]:
    """Pull a small set of FRED macro series concurrently and reduce
    each to its latest value plus 1y / 3m delta. Empty / failing
    series degrade to None so the personas can mention "macro data
    incomplete" instead of confidently citing a missing rate.

    `as_of` (PR #226): backtest mode. Forwards to FRED's
    `observation_end` so each series excludes data points after the
    backtest anchor."""
    from services import us_market_service

    async def _pull(name: str) -> tuple[str, list[dict[str, Any]]]:
        try:
            return name, await us_market_service.get_macro_indicator(
                name, as_of=as_of,
            )
        except Exception as exc:
            # `name` is a reserved LogRecord attribute (the logger name) —
            # in `extra` it raises KeyError inside logging; use a distinct key.
            log.warning("macro.fetch.failed",
                        extra={"macro_name": name, "error": str(exc)})
            return name, []

    results = await asyncio.gather(*[_pull(n) for n, _ in _MACRO_SERIES])
    by_name = dict(results)
    block: dict[str, Any] = {}
    for name, label in _MACRO_SERIES:
        block[name] = {
            "label":   label,
            "summary": _macro_summary_from_series(by_name.get(name) or []),
        }
    return block
