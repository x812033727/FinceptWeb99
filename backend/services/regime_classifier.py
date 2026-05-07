"""Market regime classifier for the timeline overlay (PR-5c).

Reads daily OHLCV + VIX archives and emits a list of regime
bands `[{start, end, regime, signals}]` the frontend paints
behind the brier line so the operator can see "did this strategy
underperform during a bear regime, or is it actually breaking?"

Four primary regimes; not mutually exclusive (a date can be
`bull + high_vol` simultaneously — both bands paint, frontend
stacks them with reduced opacity):

  - bull       : index closes above 200d MA AND RSI(14) > 50
  - bear       : index closes below 200d MA
  - high_vol   : VIX close > 25
  - low_vol    : VIX close < 15

For TW we use `_TAIEX` from `ohlcv_daily` + `tw_vix_daily`. US is
TBD (yfinance pulls aren't persisted in `ohlcv_daily` for SPX/
^VIX); when called with `market='US'` we return [] for now and
the frontend renders no overlay.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.ohlcv_daily import OhlcvDaily
from models.tw_vix_daily import TwVixDaily

log = logging.getLogger(__name__)


BULL_RSI_THRESHOLD = 50.0
HIGH_VOL_THRESHOLD = 25.0
LOW_VOL_THRESHOLD = 15.0
MA_WINDOW = 200
RSI_WINDOW = 14


def _compute_rsi_14(closes: list[float]) -> list[float | None]:
    """Wilder-smoothed RSI(14) over a closes series. Returns the
    same length as input; positions before window+1 are None."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) < RSI_WINDOW + 1:
        return out
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, RSI_WINDOW + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / RSI_WINDOW
    avg_loss = sum(losses) / RSI_WINDOW
    rs = (avg_gain / avg_loss) if avg_loss > 0 else float("inf")
    out[RSI_WINDOW] = 100.0 - (100.0 / (1.0 + rs)) if avg_loss > 0 else 100.0
    for i in range(RSI_WINDOW + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        avg_gain = (avg_gain * (RSI_WINDOW - 1) + gain) / RSI_WINDOW
        avg_loss = (avg_loss * (RSI_WINDOW - 1) + loss) / RSI_WINDOW
        if avg_loss > 0:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
        else:
            out[i] = 100.0
    return out


def _compute_ma(closes: list[float], window: int) -> list[float | None]:
    """Simple moving average; positions before window-1 are None."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) < window:
        return out
    rolling = sum(closes[:window])
    out[window - 1] = rolling / window
    for i in range(window, len(closes)):
        rolling += closes[i] - closes[i - window]
        out[i] = rolling / window
    return out


def _coalesce_bands(
    flags: list[tuple[date, str | None]],
    regime: str,
) -> list[dict[str, Any]]:
    """Walk a date-tagged flag list and merge consecutive matches
    into bands. `flags[i] = (date, regime_or_none)` — when the
    regime name matches, the day belongs to a band; otherwise it
    breaks the run."""
    bands: list[dict[str, Any]] = []
    start: date | None = None
    last_match: date | None = None
    for d, tag in flags:
        if tag == regime:
            if start is None:
                start = d
            last_match = d
        else:
            if start is not None and last_match is not None:
                bands.append({
                    "start": start.isoformat(),
                    "end": last_match.isoformat(),
                    "regime": regime,
                })
                start = None
                last_match = None
    if start is not None and last_match is not None:
        bands.append({
            "start": start.isoformat(),
            "end": last_match.isoformat(),
            "regime": regime,
        })
    return bands


async def classify_regimes(
    db: AsyncSession,
    *,
    market: str,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """Return a list of regime bands within `[start, end]` for the
    given `market`. Bands from different regimes can overlap (a
    bull day with high vol shows up in BOTH the bull and high_vol
    band lists).

    Returns `[]` for unsupported markets (US currently — until SPX
    and ^VIX get persisted into `ohlcv_daily`).
    """
    if market != "TW":
        return []

    # Pull index closes — including warmup history before `start`
    # so 200MA + RSI have enough data on the first in-window day.
    warmup_start = start - timedelta(days=MA_WINDOW + RSI_WINDOW + 14)
    rows = list((await db.scalars(
        select(OhlcvDaily)
        .where(
            OhlcvDaily.market == "TW",
            OhlcvDaily.symbol == "_TAIEX",
            OhlcvDaily.ts >= warmup_start,
            OhlcvDaily.ts <= end,
            OhlcvDaily.close.is_not(None),
        )
        .order_by(OhlcvDaily.ts.asc())
    )).all())
    if not rows:
        return []

    dates = [r.ts for r in rows]
    closes = [float(r.close) for r in rows]
    ma200 = _compute_ma(closes, MA_WINDOW)
    rsi14 = _compute_rsi_14(closes)

    vix_rows = list((await db.scalars(
        select(TwVixDaily)
        .where(
            TwVixDaily.market == "TW",
            TwVixDaily.ts >= warmup_start,
            TwVixDaily.ts <= end,
        )
        .order_by(TwVixDaily.ts.asc())
    )).all())
    vix_by_date: dict[date, float] = {
        r.ts: float(r.vix_value) for r in vix_rows
    }

    # Tag each in-window date with a flag list for each regime.
    bull_flags: list[tuple[date, str | None]] = []
    bear_flags: list[tuple[date, str | None]] = []
    high_vol_flags: list[tuple[date, str | None]] = []
    low_vol_flags: list[tuple[date, str | None]] = []
    for i, d in enumerate(dates):
        if d < start:
            continue   # warmup-only day; don't emit a band
        ma = ma200[i]
        rsi = rsi14[i]
        c = closes[i]
        if ma is not None and c < ma:
            bear_flags.append((d, "bear"))
            bull_flags.append((d, None))
        elif ma is not None and c > ma and rsi is not None and rsi > BULL_RSI_THRESHOLD:
            bull_flags.append((d, "bull"))
            bear_flags.append((d, None))
        else:
            bull_flags.append((d, None))
            bear_flags.append((d, None))

        vix = vix_by_date.get(d)
        if vix is not None and vix > HIGH_VOL_THRESHOLD:
            high_vol_flags.append((d, "high_vol"))
            low_vol_flags.append((d, None))
        elif vix is not None and vix < LOW_VOL_THRESHOLD:
            low_vol_flags.append((d, "low_vol"))
            high_vol_flags.append((d, None))
        else:
            high_vol_flags.append((d, None))
            low_vol_flags.append((d, None))

    bands = (
        _coalesce_bands(bull_flags, "bull")
        + _coalesce_bands(bear_flags, "bear")
        + _coalesce_bands(high_vol_flags, "high_vol")
        + _coalesce_bands(low_vol_flags, "low_vol")
    )
    bands.sort(key=lambda b: (b["start"], b["regime"]))
    return bands


__all__ = [
    "BULL_RSI_THRESHOLD",
    "HIGH_VOL_THRESHOLD",
    "LOW_VOL_THRESHOLD",
    "MA_WINDOW",
    "RSI_WINDOW",
    "classify_regimes",
]
