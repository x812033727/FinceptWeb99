"""Market-aware quote recency checks for simulated execution."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class QuoteFreshness:
    is_fresh: bool
    reason: str
    quote_time: datetime | None
    age_seconds: float | None


_MAX_AGE_SECONDS = {"US": 90, "TW": 90, "CRYPTO": 30}
_INTRADAY_SOURCES = {
    "US": frozenset({"polygon", "yfinance", "finnhub"}),
    "TW": frozenset({"twse_mis"}),
    "CRYPTO": frozenset({"kraken"}),
}
_FUTURE_TOLERANCE_SECONDS = 30


def parse_quote_time(value) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else None
    if isinstance(value, (int, float)):
        numeric = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            numeric = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed.astimezone(UTC) if parsed.tzinfo else None
    else:
        return None

    if not math.isfinite(numeric) or numeric <= 0:
        return None
    absolute = abs(numeric)
    if absolute >= 1e17:
        numeric /= 1e9  # nanoseconds
    elif absolute >= 1e14:
        numeric /= 1e6  # microseconds
    elif absolute >= 1e11:
        numeric /= 1e3  # milliseconds
    try:
        return datetime.fromtimestamp(numeric, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def quote_time(quote: dict) -> datetime | None:
    for field in ("ts", "timestamp", "fetched_at"):
        parsed = parse_quote_time(quote.get(field))
        if parsed is not None:
            return parsed
    return None


def assess_quote_freshness(market: str, quote: dict, *, now: datetime) -> QuoteFreshness:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if market not in _MAX_AGE_SECONDS:
        raise ValueError(f"unsupported market {market}")

    source = str(quote.get("data_source") or "").lower()
    if not source:
        return QuoteFreshness(False, "missing_source", None, None)
    if source == "unavailable":
        return QuoteFreshness(False, "source_unavailable", None, None)
    if source and source not in _INTRADAY_SOURCES[market]:
        return QuoteFreshness(False, "non_executable_source", None, None)

    observed_at = quote_time(quote)
    if observed_at is None:
        return QuoteFreshness(False, "missing_timestamp", None, None)
    age = (now.astimezone(UTC) - observed_at).total_seconds()
    if age < -_FUTURE_TOLERANCE_SECONDS:
        return QuoteFreshness(False, "future_timestamp", observed_at, age)
    if age > _MAX_AGE_SECONDS[market]:
        return QuoteFreshness(False, "stale_timestamp", observed_at, age)
    return QuoteFreshness(True, "fresh", observed_at, age)
