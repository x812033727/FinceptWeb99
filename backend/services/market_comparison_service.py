"""Cross-market relative-performance comparison for 2-5 instruments."""
from __future__ import annotations

import asyncio
import math
import re
from datetime import date, datetime, timedelta, timezone
from statistics import stdev
from typing import Any

from services.crypto_market_service import get_history as crypto_history
from services.tw_market_service import get_history as tw_history
from services.us_market_service import get_history as us_history

PERIOD_DAYS = {"1m": 30, "3m": 90, "6m": 180, "1y": 365}
_SYMBOL = re.compile(r"^[A-Z0-9.\-]{1,20}$")


def parse_instruments(raw: str) -> list[tuple[str, str]]:
    values = [value.strip().upper() for value in raw.split(",") if value.strip()]
    if not 2 <= len(values) <= 5:
        raise ValueError("Select between 2 and 5 instruments")
    parsed: list[tuple[str, str]] = []
    for value in values:
        if ":" not in value:
            raise ValueError(f"Invalid instrument {value!r}; use MARKET:SYMBOL")
        market, symbol = value.split(":", 1)
        if market not in {"TW", "US", "CRYPTO"} or not _SYMBOL.fullmatch(symbol):
            raise ValueError(f"Invalid instrument {value!r}")
        item = (market, symbol)
        if item in parsed:
            raise ValueError(f"Duplicate instrument {value!r}")
        parsed.append(item)
    return parsed


def _date(raw: Any) -> date | None:
    if isinstance(raw, (int, float)):
        seconds = float(raw) / 1000 if raw > 10_000_000_000 else float(raw)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date()
        except (ValueError, OSError, OverflowError):
            return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


async def _fetch(market: str, symbol: str, period: str) -> list[dict[str, Any]]:
    if market == "US":
        return await us_history(symbol, period=period, interval="1d")
    if market == "TW":
        return await tw_history(symbol, months={"1m": 2, "3m": 4, "6m": 7, "1y": 13}[period])
    return await crypto_history(symbol, interval="1d", limit=PERIOD_DAYS[period] + 10)


def _clean(rows: list[dict[str, Any]], cutoff: date) -> list[tuple[date, float, str | None]]:
    by_day: dict[date, tuple[float, str | None]] = {}
    for row in rows:
        day = _date(row.get("date") or row.get("time") or row.get("timestamp"))
        close = row.get("close")
        try:
            price = float(close)
        except (TypeError, ValueError):
            continue
        if day is not None and day >= cutoff and math.isfinite(price) and price > 0:
            by_day[day] = (price, row.get("data_source"))
    return [(day, *by_day[day]) for day in sorted(by_day)]


def _metrics(prices: list[float], market: str) -> tuple[float, float, float | None]:
    total_return = (prices[-1] / prices[0] - 1) * 100
    peak = prices[0]
    max_drawdown = 0.0
    for price in prices:
        peak = max(peak, price)
        max_drawdown = min(max_drawdown, price / peak - 1)
    returns = [prices[i] / prices[i - 1] - 1 for i in range(1, len(prices))]
    annualised = None
    if len(returns) >= 2:
        annualised = stdev(returns) * math.sqrt(365 if market == "CRYPTO" else 252) * 100
    return total_return, max_drawdown * 100, annualised


async def compare_history(raw_instruments: str, period: str) -> dict[str, Any]:
    if period not in PERIOD_DAYS:
        raise ValueError(f"period must be one of {sorted(PERIOD_DAYS)}")
    instruments = parse_instruments(raw_instruments)
    cutoff = date.today() - timedelta(days=PERIOD_DAYS[period])
    fetched = await asyncio.gather(
        *[_fetch(market, symbol, period) for market, symbol in instruments],
        return_exceptions=True,
    )
    cleaned: list[tuple[str, str, list[tuple[date, float, str | None]]]] = []
    excluded: list[dict[str, str]] = []
    for (market, symbol), result in zip(instruments, fetched):
        if isinstance(result, BaseException):
            excluded.append({"market": market, "symbol": symbol, "reason": "provider_unavailable"})
            continue
        rows = _clean(result, cutoff)
        if len(rows) < 2:
            excluded.append({"market": market, "symbol": symbol, "reason": "insufficient_history"})
            continue
        cleaned.append((market, symbol, rows))

    common_base = max((rows[0][0] for _, _, rows in cleaned), default=None)
    series = []
    if common_base is not None:
        for market, symbol, rows in cleaned:
            aligned = [row for row in rows if row[0] >= common_base]
            if len(aligned) < 2:
                excluded.append({"market": market, "symbol": symbol, "reason": "no_common_window"})
                continue
            base = aligned[0][1]
            prices = [row[1] for row in aligned]
            total_return, drawdown, volatility = _metrics(prices, market)
            series.append({
                "instrument": f"{market}:{symbol}", "market": market, "symbol": symbol,
                "base_date": aligned[0][0], "end_date": aligned[-1][0],
                "observations": len(aligned),
                "return_pct": round(total_return, 4),
                "max_drawdown_pct": round(drawdown, 4),
                "annualised_volatility_pct": round(volatility, 4) if volatility is not None else None,
                "data_source": aligned[-1][2],
                "points": [
                    {"date": day, "value": round(price / base * 100, 4)}
                    for day, price, _ in aligned
                ],
            })
    return {
        "period": period,
        "requested": [f"{market}:{symbol}" for market, symbol in instruments],
        "common_base_date": common_base,
        "normalization": "first_available_close_equals_100",
        "currency_note": "Returns are measured in each instrument's native quote currency; FX is not converted.",
        "series": series,
        "excluded": excluded,
    }
