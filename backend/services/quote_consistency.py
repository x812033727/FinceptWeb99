"""Cross-provider quote consistency checks.

The market services decide which independent secondary provider to query.
This module owns the provider-agnostic comparison contract and metrics so TW
and US responses use the same semantics.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Literal

from middleware.metrics import MARKET_DATA_CONSISTENCY_CHECKS_TOTAL

ConsistencyStatus = Literal["verified", "conflict", "unverified"]


def unverified_check(
    primary_source: str,
    *,
    secondary_source: str | None = None,
    flag: str,
    market: str,
) -> dict[str, Any]:
    secondary = secondary_source or "none"
    MARKET_DATA_CONSISTENCY_CHECKS_TOTAL.labels(
        market=market.lower(), status="unverified",
        primary=primary_source or "unknown", secondary=secondary,
    ).inc()
    return {
        "status": "unverified",
        "primary_source": primary_source or "unknown",
        "secondary_source": secondary_source,
        "spread_pct": None,
        "observations": {},
        "checked_at": datetime.now(UTC).isoformat(),
        "flags": [flag],
    }


def compare_prices(
    *,
    market: str,
    primary_source: str,
    primary_price: Any,
    secondary_source: str,
    secondary_price: Any,
    max_spread_pct: float,
) -> dict[str, Any]:
    """Compare two positive prices using a symmetric percentage spread."""
    try:
        first = float(primary_price)
        second = float(secondary_price)
    except (TypeError, ValueError):
        return unverified_check(
            primary_source, secondary_source=secondary_source,
            flag="invalid_price_observation", market=market,
        )
    if not all(math.isfinite(value) and value > 0 for value in (first, second)):
        return unverified_check(
            primary_source, secondary_source=secondary_source,
            flag="invalid_price_observation", market=market,
        )

    midpoint = (first + second) / 2
    spread_pct = abs(first - second) / midpoint * 100
    status: ConsistencyStatus = "conflict" if spread_pct > max_spread_pct else "verified"
    MARKET_DATA_CONSISTENCY_CHECKS_TOTAL.labels(
        market=market.lower(), status=status,
        primary=primary_source, secondary=secondary_source,
    ).inc()
    return {
        "status": status,
        "primary_source": primary_source,
        "secondary_source": secondary_source,
        "spread_pct": round(spread_pct, 4),
        "observations": {
            primary_source: first,
            secondary_source: second,
        },
        "checked_at": datetime.now(UTC).isoformat(),
        "flags": ["price_source_conflict"] if status == "conflict" else [],
    }
