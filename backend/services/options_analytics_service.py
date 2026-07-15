"""Evidence-bounded U.S. options-chain analytics.

All calculations are deterministic and abstain on missing fields. In
particular, this module does not back-solve IV, invent OI, or label a
moneyness proxy as 25-delta skew. The exposed wing skew is explicitly the
nearest 90%-moneyness put IV minus the nearest 110%-moneyness call IV.
"""
from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any

from middleware.metrics import OPTIONS_ANALYSIS_TOTAL
from services import us_market_service


def _number(value: Any, *, minimum: float | None = None, maximum: float | None = None) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    if minimum is not None and result < minimum:
        return None
    if maximum is not None and result > maximum:
        return None
    return result


def _integer(value: Any) -> int | None:
    number = _number(value, minimum=0)
    return int(number) if number is not None else None


def _expiry(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _nearest_iv(rows: list[dict[str, Any]], target: float, kind: str) -> tuple[float | None, float | None]:
    candidates = [
        row for row in rows
        if row["contract_type"] == kind and row["implied_volatility"] is not None
    ]
    if not candidates:
        return None, None
    selected = min(candidates, key=lambda row: abs(row["strike_price"] - target))
    return selected["implied_volatility"], selected["strike_price"]


def _max_pain(rows: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    with_oi = [row for row in rows if (row["open_interest"] or 0) > 0]
    strikes = sorted({row["strike_price"] for row in with_oi})
    if not strikes:
        return None, None
    best_strike: float | None = None
    best_payout: float | None = None
    for settlement in strikes:
        payout = 0.0
        for row in with_oi:
            intrinsic = (
                max(0.0, settlement - row["strike_price"])
                if row["contract_type"] == "call"
                else max(0.0, row["strike_price"] - settlement)
            )
            payout += intrinsic * (row["open_interest"] or 0) * 100
        if best_payout is None or payout < best_payout:
            best_strike, best_payout = settlement, payout
    return best_strike, best_payout


def analyze_options_chain(
    symbol: str,
    raw_contracts: list[dict[str, Any]],
    *,
    spot: float | None,
    spot_source: str | None = None,
    as_of: date | None = None,
    max_expiries: int = 8,
) -> dict[str, Any]:
    today = as_of or datetime.now(UTC).date()
    provider_truncated = any(bool(row.get("chain_truncated")) for row in raw_contracts)
    clean: list[dict[str, Any]] = []
    expired_rows = 0
    for raw in raw_contracts:
        kind = str(raw.get("contract_type") or "").lower()
        strike = _number(raw.get("strike_price"), minimum=0.000001)
        expiry = _expiry(raw.get("expiration_date"))
        if kind not in {"call", "put"} or strike is None or expiry is None:
            continue
        if expiry < today:
            expired_rows += 1
            continue
        iv = _number(raw.get("implied_volatility"), minimum=0.000001, maximum=10)
        oi = _integer(raw.get("open_interest"))
        volume = _integer(raw.get("volume"))
        clean.append({
            "ticker": str(raw.get("ticker") or ""),
            "contract_type": kind,
            "expiration_date": expiry.isoformat(),
            "strike_price": strike,
            "last_price": _number(raw.get("last_price"), minimum=0),
            "bid": _number(raw.get("bid"), minimum=0),
            "ask": _number(raw.get("ask"), minimum=0),
            "volume": volume,
            "open_interest": oi,
            "implied_volatility": iv,
            "delta": _number(raw.get("delta"), minimum=-1, maximum=1),
            "gamma": _number(raw.get("gamma"), minimum=0),
            "theta": _number(raw.get("theta")),
            "vega": _number(raw.get("vega"), minimum=0),
            "data_source": str(raw.get("data_source") or "unknown"),
        })

    expiries_all = sorted({row["expiration_date"] for row in clean})
    selected_expiries = expiries_all[:max_expiries]
    limited = len(expiries_all) > len(selected_expiries)
    clean = [row for row in clean if row["expiration_date"] in selected_expiries]
    clean.sort(key=lambda row: (row["expiration_date"], row["strike_price"], row["contract_type"]))

    normalized_spot = _number(spot, minimum=0.000001)
    if normalized_spot is None:
        normalized_spot = next((
            _number(row.get("underlying_price"), minimum=0.000001)
            for row in raw_contracts
            if _number(row.get("underlying_price"), minimum=0.000001) is not None
        ), None)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in clean:
        grouped[row["expiration_date"]].append(row)

    expiry_analytics: list[dict[str, Any]] = []
    for expiration in selected_expiries:
        rows = grouped[expiration]
        dte = (date.fromisoformat(expiration) - today).days
        call_rows = [row for row in rows if row["contract_type"] == "call"]
        put_rows = [row for row in rows if row["contract_type"] == "put"]
        call_oi = sum(row["open_interest"] or 0 for row in call_rows)
        put_oi = sum(row["open_interest"] or 0 for row in put_rows)
        call_volume = sum(row["volume"] or 0 for row in call_rows)
        put_volume = sum(row["volume"] or 0 for row in put_rows)

        atm_call_iv = atm_put_iv = atm_call_strike = atm_put_strike = None
        put_90_iv = call_110_iv = put_90_strike = call_110_strike = None
        if normalized_spot is not None:
            atm_call_iv, atm_call_strike = _nearest_iv(rows, normalized_spot, "call")
            atm_put_iv, atm_put_strike = _nearest_iv(rows, normalized_spot, "put")
            put_90_iv, put_90_strike = _nearest_iv(rows, normalized_spot * 0.9, "put")
            call_110_iv, call_110_strike = _nearest_iv(rows, normalized_spot * 1.1, "call")
        atm_values = [value for value in (atm_call_iv, atm_put_iv) if value is not None]
        atm_iv = sum(atm_values) / len(atm_values) if atm_values else None
        expected_move_pct = (
            atm_iv * math.sqrt(max(dte, 1) / 365)
            if atm_iv is not None else None
        )
        max_pain, max_pain_payout = _max_pain(rows)
        expiry_analytics.append({
            "expiration_date": expiration,
            "days_to_expiry": dte,
            "contract_count": len(rows),
            "call_open_interest": call_oi,
            "put_open_interest": put_oi,
            "put_call_open_interest_ratio": put_oi / call_oi if call_oi else None,
            "call_volume": call_volume,
            "put_volume": put_volume,
            "put_call_volume_ratio": put_volume / call_volume if call_volume else None,
            "atm_iv": atm_iv,
            "atm_call_iv": atm_call_iv,
            "atm_put_iv": atm_put_iv,
            "atm_call_strike": atm_call_strike,
            "atm_put_strike": atm_put_strike,
            "expected_move": normalized_spot * expected_move_pct if normalized_spot and expected_move_pct is not None else None,
            "expected_move_pct": expected_move_pct,
            "put_90_iv": put_90_iv,
            "put_90_strike": put_90_strike,
            "call_110_iv": call_110_iv,
            "call_110_strike": call_110_strike,
            "wing_skew_iv_points": (put_90_iv - call_110_iv) * 100 if put_90_iv is not None and call_110_iv is not None else None,
            "max_pain": max_pain,
            "max_pain_distance_pct": (max_pain / normalized_spot - 1) * 100 if max_pain and normalized_spot else None,
            "max_pain_total_payout": max_pain_payout,
        })

    iv_count = sum(row["implied_volatility"] is not None for row in clean)
    oi_count = sum(row["open_interest"] is not None for row in clean)
    usable_count = len(clean)
    iv_coverage = iv_count / usable_count * 100 if usable_count else 0.0
    oi_coverage = oi_count / usable_count * 100 if usable_count else 0.0
    flags: list[str] = []
    if not usable_count:
        flags.append("no_usable_contracts")
    if normalized_spot is None:
        flags.append("spot_unavailable")
    if usable_count and iv_coverage < 50:
        flags.append("iv_sparse")
    if usable_count and oi_coverage < 50:
        flags.append("open_interest_sparse")
    if expired_rows:
        flags.append("expired_rows_dropped")
    if limited:
        flags.append("expiry_window_limited")
    if provider_truncated:
        flags.append("provider_page_cap_reached")
    sources = sorted({row["data_source"] for row in clean})
    degrading_flags = {"spot_unavailable", "iv_sparse", "open_interest_sparse"}
    status = (
        "unavailable" if not usable_count
        else "degraded" if any(flag in degrading_flags for flag in flags)
        else "good"
    )
    return {
        "symbol": symbol.upper(),
        "spot": normalized_spot,
        "spot_source": spot_source,
        "as_of": today.isoformat(),
        "contracts": clean,
        "expiries": expiry_analytics,
        "quality": {
            "status": status,
            "flags": flags,
            "sources": sources,
            "rows_received": len(raw_contracts),
            "rows_usable": usable_count,
            "iv_coverage_pct": round(iv_coverage, 2),
            "open_interest_coverage_pct": round(oi_coverage, 2),
        },
        "methodology": {
            "version": "options-chain-analytics-v1",
            "atm_iv": "mean of nearest-strike call and put IV when available",
            "wing_skew": "nearest 90%-moneyness put IV minus nearest 110%-moneyness call IV; not 25-delta skew",
            "expected_move": "spot × ATM IV × sqrt(max(calendar DTE, 1) / 365)",
            "max_pain": "strike minimizing aggregate intrinsic payout weighted by reported open interest",
        },
    }


async def get_options_analysis(symbol: str, *, max_expiries: int = 8) -> dict[str, Any]:
    contracts, quote = await asyncio.gather(
        us_market_service.get_options(symbol.upper(), max_expiries=max_expiries),
        us_market_service.get_quote(symbol.upper()),
    )
    spot = quote.get("price") if quote else None
    result = analyze_options_chain(
        symbol, contracts,
        spot=spot,
        spot_source=quote.get("data_source") if quote else None,
        max_expiries=max_expiries,
    )
    OPTIONS_ANALYSIS_TOTAL.labels(outcome=result["quality"]["status"]).inc()
    return result
