"""Shared additive data-quality metadata for market responses."""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Freshness = Literal["fresh", "stale", "unavailable", "unknown"]
Consistency = Literal["verified", "conflict", "unverified"]


class DataQualityMeta(BaseModel):
    source: str
    as_of: str | None = None
    market_session: str
    freshness: Freshness
    fallback_chain: list[str] = Field(default_factory=list)
    consistency: Consistency = "unverified"
    price_spread_pct: float | None = None
    cross_checked_sources: list[str] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)
    checked_at: str | None = None


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        # Quote timestamps are Unix milliseconds.
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, UTC)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
        except ValueError:
            return None
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def build_quality_meta(
    payload: dict[str, Any],
    *,
    kind: Literal["quote", "history", "fundamentals", "dataset"],
    fallback_chain: list[str],
    as_of: Any = None,
) -> DataQualityMeta:
    source = str(payload.get("data_source") or payload.get("source") or "unknown")
    anchor = as_of or payload.get("as_of_session") or payload.get("as_of") \
        or payload.get("fetched_at") or payload.get("ts")
    anchor_dt = _as_datetime(anchor)
    anchor_text = anchor_dt.isoformat() if anchor_dt else (str(anchor) if anchor else None)

    meaningful = {
        "quote": bool(payload.get("price")),
        "history": bool(payload.get("close")),
        "fundamentals": any(payload.get(k) is not None for k in (
            "market_cap", "pe_ratio", "pb_ratio", "eps", "dividend_yield",
        )),
        "dataset": any(
            value not in (None, "", [], {})
            for key, value in payload.items()
            if key not in {"data_source", "source", "as_of", "date", "published_at"}
        ),
    }[kind]
    if source == "unavailable" or not meaningful:
        freshness: Freshness = "unavailable"
    elif payload.get("is_stale") is True or "stale" in source:
        freshness = "stale"
    elif anchor_dt is None:
        freshness = "unknown"
    else:
        age = (datetime.now(UTC) - anchor_dt).total_seconds()
        threshold = {"quote": 4 * 86400, "history": 5 * 86400, "fundamentals": 8 * 86400, "dataset": 8 * 86400}[kind]
        freshness = "stale" if age > threshold else ("unknown" if source == "unknown" else "fresh")

    if kind == "history":
        market_session = "historical"
    elif payload.get("is_market_open") is True:
        market_session = "regular"
    elif payload.get("is_market_open") is False:
        market_session = "closed"
    else:
        market_session = "unknown"

    quality_check = payload.get("quality_check")
    if not isinstance(quality_check, dict):
        quality_check = {}
    consistency = quality_check.get("status", "unverified")
    if consistency not in {"verified", "conflict", "unverified"}:
        consistency = "unverified"
    checked_sources = [
        str(value) for value in (
            quality_check.get("primary_source"), quality_check.get("secondary_source"),
        ) if value
    ]

    return DataQualityMeta(
        source=source,
        as_of=anchor_text,
        market_session=market_session,
        freshness=freshness,
        fallback_chain=fallback_chain,
        consistency=consistency,
        price_spread_pct=quality_check.get("spread_pct"),
        cross_checked_sources=checked_sources,
        quality_flags=[str(flag) for flag in quality_check.get("flags", [])],
        checked_at=quality_check.get("checked_at"),
    )
