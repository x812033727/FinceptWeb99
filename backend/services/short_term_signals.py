"""Short-term technical signals computed from `ohlcv_daily`.

Pure-compute module — reads OHLCV bars, returns a typed metrics dict.
No HTTP, no caching, no LLM. Used by the discussion context builder
to surface short-term-prediction context (1-5 trading-day horizon)
for each focus symbol so personas can ground their analysis in
quantitative signals rather than narrative-only reasoning.

Metrics (all float, all % where applicable):

  volume_ratio
      Latest day volume divided by the trailing 20-day mean
      (excluding today). > 2.0 historically marks a directional
      breakout day; > 5.0 borders speculative blow-off.

  return_5d / return_20d
      Cumulative close-to-close % return over the lookback. Used in
      tandem: positive 5d + positive 20d = sustained uptrend;
      positive 5d + negative 20d = potential reversal attempt.

  rsi_14
      Wilder's RSI over a 14-day window. Standard 30/70 thresholds
      apply (oversold / overbought). Uses simple moving average of
      gains/losses (the "first cut" Wilder formulation, not the
      smoothed exponential variant — the difference washes out at
      14 periods and the simple version is easier to verify).

  gap_pct
      (today_open - yesterday_close) / yesterday_close * 100. Useful
      with volume_ratio: a > 2% gap on > 2x volume usually signals
      news-driven repricing, not noise.

Insufficient data: returns None when fewer than 21 bars are
available (need 20 for averages + today). Caller should drop the
symbol from the block rather than emitting partial metrics.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from services.ingest.repository import read_ohlcv_range

log = logging.getLogger(__name__)

# Need at least 21 bars: 20 for the rolling-mean window + 1 for "today".
_MIN_BARS = 21
# Read this many calendar days back to leave room for weekends + holidays
# while still hitting >= 21 trading days. ~45 days covers the worst case
# (CNY week + a one-week holiday cluster).
_LOOKBACK_DAYS = 45


@dataclass(frozen=True)
class ShortTermSignals:
    symbol: str
    as_of: str
    close: float
    volume_ratio: float | None
    return_5d: float | None
    return_20d: float | None
    rsi_14: float | None
    gap_pct: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol":       self.symbol,
            "as_of":        self.as_of,
            "close":        round(self.close, 4),
            "volume_ratio": _round_or_none(self.volume_ratio, 2),
            "return_5d":    _round_or_none(self.return_5d, 2),
            "return_20d":   _round_or_none(self.return_20d, 2),
            "rsi_14":       _round_or_none(self.rsi_14, 1),
            "gap_pct":      _round_or_none(self.gap_pct, 2),
        }


def _round_or_none(v: float | None, digits: int) -> float | None:
    return None if v is None else round(v, digits)


def _compute_rsi_14(closes: list[float]) -> float | None:
    """Wilder RSI over the last 14 closes' deltas. Needs >= 15 closes
    to produce 14 deltas."""
    if len(closes) < 15:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(-14, 0)]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_gain = sum(gains) / 14 if gains else 0.0
    avg_loss = sum(losses) / 14 if losses else 0.0
    if avg_loss == 0:
        # All up days — formally RSI=100; capped by Wilder convention.
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_volume_ratio(
    today_volume: int | None, prior_volumes: list[int | None],
) -> float | None:
    """Today's volume divided by the mean of the prior N volumes
    (caller provides the trailing window already excluding today).
    Returns None when today's volume is missing OR the average is
    zero (a freshly-IPO'd stock with one trading day of history)."""
    if today_volume is None:
        return None
    valid = [v for v in prior_volumes if v is not None]
    if not valid:
        return None
    mean = sum(valid) / len(valid)
    if mean <= 0:
        return None
    return today_volume / mean


async def compute_short_term_signals(
    db: AsyncSession,
    *,
    market: str,
    symbol: str,
    as_of: date | None = None,
) -> dict[str, Any] | None:
    """Compute the five Tier-1 short-term metrics for `symbol`. Returns
    None when:
      - the archive holds fewer than `_MIN_BARS` rows in the lookback,
      - or the most recent bar's close/volume is missing.

    `as_of=None` means "use today (UTC)" — live mode. Backtest discussions
    pass the anchor date so the metrics reflect what the personas
    *would have seen* at that point, not present-day data.
    """
    end = as_of or datetime.now(UTC).date()
    start = end - timedelta(days=_LOOKBACK_DAYS)

    try:
        bars = await read_ohlcv_range(db, market, symbol, start, end)
    except Exception as exc:
        log.warning(
            "short_term_signals.read_failed",
            extra={"symbol": symbol, "error": str(exc)},
        )
        return None

    if len(bars) < _MIN_BARS:
        return None

    # Bars are returned ascending; latest is the last entry.
    latest = bars[-1]
    prev = bars[-2]
    close = latest.get("close")
    if close is None:
        return None

    closes = [b.get("close") for b in bars if b.get("close") is not None]
    if len(closes) < _MIN_BARS:
        return None

    today_volume = latest.get("volume")
    prior_volumes = [b.get("volume") for b in bars[-21:-1]]   # 20 prior days
    volume_ratio = _compute_volume_ratio(today_volume, prior_volumes)

    # Returns: indices align with `closes` (filtered to non-null).
    return_5d: float | None = None
    return_20d: float | None = None
    if len(closes) >= 6 and closes[-6] not in (0, None):
        return_5d = (closes[-1] / closes[-6] - 1) * 100
    if len(closes) >= 21 and closes[-21] not in (0, None):
        return_20d = (closes[-1] / closes[-21] - 1) * 100

    rsi_14 = _compute_rsi_14(closes)

    # Gap: today's open vs prev close.
    today_open = latest.get("open")
    prev_close = prev.get("close")
    gap_pct: float | None = None
    if today_open is not None and prev_close not in (None, 0):
        gap_pct = (today_open - prev_close) / prev_close * 100

    signals = ShortTermSignals(
        symbol=symbol,
        as_of=str(latest.get("time") or end.isoformat()),
        close=float(close),
        volume_ratio=volume_ratio,
        return_5d=return_5d,
        return_20d=return_20d,
        rsi_14=rsi_14,
        gap_pct=gap_pct,
    )
    return signals.to_dict()
