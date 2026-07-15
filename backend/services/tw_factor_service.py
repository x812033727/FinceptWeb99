"""Explainable Taiwan-equity multi-factor ranking and rolling validation.

The model is deliberately deterministic: no fitted parameters and no "ML"
label.  Every score is a winsorised cross-sectional z-score built only from
snapshots whose dates are at or before ``as_of``. Missing factors are
renormalised only when at least 60% of the selected profile weight is present;
otherwise the stock abstains.

Known evidence limits are returned in ``quality.flags`` rather than hidden.
Return factors prefer FinMind's corporate-action-adjusted close side-car while
liquidity and the displayed price continue to use the raw traded close.  A
historical universe is reconstructed from listing/delisting dates, with the
earliest archived bar as a conservative listing-date fallback.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta
from random import Random
from statistics import fmean, median, pstdev, stdev
from typing import Any

from scipy.stats import t as student_t
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cache.cache_ttls import TTL_FACTOR_RANKING, TTL_FACTOR_VALIDATION
from cache.redis_cache import (
    cache_get_json,
    cache_set_json,
    key_factor_ranking_tw,
    key_factor_validation_tw,
)
from db.session import AsyncSessionLocal
from middleware.metrics import FACTOR_ANALYSIS_TOTAL
from models.fundamentals_snapshot import FundamentalsSnapshot
from models.ohlcv_daily import OhlcvDaily
from models.tw_company_classification_snapshot import TwCompanyClassificationSnapshot
from models.tw_company_info import TwCompanyInfo
from services.tw_symbol_service import is_etf

METHOD_VERSION = "tw-explainable-multifactor-v9"
FACTOR_NAMES = ("value", "quality", "momentum", "low_volatility", "income", "liquidity")
QUALITY_COMPONENTS = ("operating_margin", "return_on_assets", "cash_return_on_assets", "balance_strength")
PROFILES: dict[str, dict[str, float]] = {
    "balanced": {"value": 0.25, "quality": 0.15, "momentum": 0.20,
                 "low_volatility": 0.15, "income": 0.10, "liquidity": 0.15},
    "value": {"value": 0.45, "quality": 0.15, "momentum": 0.10,
              "low_volatility": 0.10, "income": 0.10, "liquidity": 0.10},
    "momentum": {"value": 0.15, "quality": 0.10, "momentum": 0.45,
                 "low_volatility": 0.15, "income": 0.05, "liquidity": 0.10},
    "defensive": {"value": 0.15, "quality": 0.20, "momentum": 0.10,
                  "low_volatility": 0.35, "income": 0.10, "liquidity": 0.10},
    "income": {"value": 0.15, "quality": 0.15, "momentum": 0.10,
               "low_volatility": 0.15, "income": 0.35, "liquidity": 0.10},
}
MIN_WEIGHT_COVERAGE = 0.60
MIN_SECTOR_GROUP_SIZE = 2
MIN_SECTOR_COVERAGE = 0.60
MAX_EXECUTION_DEFERRAL_SESSIONS = 5
BENCHMARKS = ("taiex_total_return", "equal_weight")
WEIGHT_MODES = ("fixed", "walk_forward")
DIAGNOSTIC_SIGNALS = ("composite", *FACTOR_NAMES)


def _number(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or (positive and result <= 0):
        return None
    return result


def _percentile(sorted_values: list[float], q: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = q * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction


def winsorized_zscores(values: dict[str, float | None]) -> dict[str, float]:
    """5/95 winsorisation followed by population z-score.

    A constant cross-section returns zero for every valid observation instead
    of dividing by zero. Missing/non-finite observations remain absent.
    """
    clean = {symbol: number for symbol, value in values.items()
             if (number := _number(value)) is not None}
    if not clean:
        return {}
    ordered = sorted(clean.values())
    low, high = _percentile(ordered, 0.05), _percentile(ordered, 0.95)
    clipped = {symbol: min(high, max(low, value)) for symbol, value in clean.items()}
    mean = fmean(clipped.values())
    std = pstdev(clipped.values())
    if std == 0:
        return {symbol: 0.0 for symbol in clipped}
    return {symbol: (value - mean) / std for symbol, value in clipped.items()}


def _price_factors(bars: list[dict[str, Any]]) -> dict[str, float | None]:
    clean = [
        {"date": str(row.get("date") or row.get("time")),
         "close": _number(row.get("close"), positive=True),
         "raw_close": _number(row.get("raw_close", row.get("close")), positive=True),
         "adjusted": bool(row.get("adjusted")),
         "volume": _number(row.get("volume"), positive=True)}
        for row in bars
    ]
    clean = [row for row in clean if row["close"] is not None]
    clean.sort(key=lambda row: row["date"])
    momentum: float | None = None
    if len(clean) >= 148:
        start, end = clean[-148]["close"], clean[-22]["close"]
        momentum = end / start - 1 if start and end else None

    volatility: float | None = None
    if len(clean) >= 64:
        closes = [float(row["close"]) for row in clean[-64:]]
        returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        volatility = pstdev(returns) * math.sqrt(252) if len(returns) >= 2 else None

    dollar_volumes = [
        float(row["raw_close"]) * float(row["volume"])
        for row in clean[-20:] if row["volume"] is not None and row["raw_close"] is not None
    ]
    liquidity = math.log(fmean(dollar_volumes)) if len(dollar_volumes) >= 10 else None
    adjusted_observations = sum(bool(row["adjusted"]) for row in clean)
    return {
        "momentum": momentum,
        # Higher scores must always mean "more" of the desired property.
        "low_volatility": -volatility if volatility is not None else None,
        "liquidity": liquidity,
        "latest_close": clean[-1]["raw_close"] if clean else None,
        "latest_session": clean[-1]["date"] if clean else None,
        "observations": len(clean),
        "adjusted_observations": adjusted_observations,
    }


def merge_adjusted_prices(
    bars_by_symbol: dict[str, list[dict[str, Any]]],
    adjusted_by_symbol: dict[str, dict[str, float]],
) -> dict[str, list[dict[str, Any]]]:
    """Overlay adjusted closes without mutating the canonical raw bars."""
    merged: dict[str, list[dict[str, Any]]] = {}
    for symbol in sorted(set(bars_by_symbol) | set(adjusted_by_symbol)):
        rows = bars_by_symbol.get(symbol, [])
        adjusted = adjusted_by_symbol.get(symbol, {})
        merged[symbol] = []
        seen_sessions: set[str] = set()
        for row in rows:
            session = str(row.get("date") or row.get("time") or "")[:10]
            seen_sessions.add(session)
            raw_close = _number(row.get("raw_close", row.get("close")), positive=True)
            adj_close = _number(adjusted.get(session), positive=True)
            merged[symbol].append({
                **row,
                "raw_close": raw_close,
                "close": adj_close if adj_close is not None else raw_close,
                "adjusted": adj_close is not None,
            })
        # Delisted symbols may no longer exist in the main app's current-
        # universe OHLCV archive. Preserve their adjusted history for return
        # and volatility factors; volume-based liquidity abstains naturally.
        for session, value in adjusted.items():
            if session in seen_sessions:
                continue
            adj_close = _number(value, positive=True)
            if adj_close is None:
                continue
            merged[symbol].append({
                "date": session, "close": adj_close, "raw_close": adj_close,
                "volume": None, "adjusted": True,
                "raw_price_unavailable": True,
            })
        merged[symbol].sort(key=lambda row: str(row.get("date") or row.get("time") or ""))
    return merged


def point_in_time_companies(
    *,
    as_of: date,
    current: dict[str, dict[str, Any]],
    stock_info: dict[str, dict[str, Any]],
    delistings: dict[str, date],
    bars_by_symbol: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Build the investable company map as it existed at ``as_of``.

    The first archived price is a conservative listing-date proxy when the
    master dataset has no listing date. It may exclude an otherwise eligible
    name, but cannot introduce it before evidence of trading exists.
    """
    result: dict[str, dict[str, Any]] = {}
    symbols = set(current) | set(stock_info) | set(delistings) | set(bars_by_symbol)
    for symbol in symbols:
        info = dict(current.get(symbol, {}))
        info.update({key: value for key, value in stock_info.get(symbol, {}).items()
                     if value is not None})
        listed_at = info.get("listed_at")
        if isinstance(listed_at, str):
            try:
                listed_at = date.fromisoformat(listed_at[:10])
            except ValueError:
                listed_at = None
        if listed_at is None:
            sessions = sorted(
                str(row.get("date") or row.get("time") or "")[:10]
                for row in bars_by_symbol.get(symbol, [])
                if row.get("date") or row.get("time")
            )
            if sessions:
                try:
                    listed_at = date.fromisoformat(sessions[0])
                    info["listing_date_source"] = "earliest_archived_price"
                except ValueError:
                    listed_at = None
        delisted_at = delistings.get(symbol)
        if listed_at is not None and listed_at > as_of:
            continue
        if delisted_at is not None and delisted_at < as_of:
            continue
        info["listed_at"] = listed_at.isoformat() if listed_at else None
        info["delisted_at"] = delisted_at.isoformat() if delisted_at else None
        result[symbol] = info
    return result


def point_in_time_universe_ready(
    *,
    catalog_ready: bool,
    as_of: date,
    delistings: dict[str, date],
    fundamentals: dict[str, Any],
    bars_by_symbol: dict[str, list[dict[str, Any]]],
) -> bool:
    """Only claim survivorship coverage when relevant delisted names have data."""
    if not catalog_ready:
        return False
    relevant_delisted = {
        symbol for symbol, delisted_at in delistings.items()
        if delisted_at >= as_of
    }
    fresh_fundamentals: set[str] = set()
    for symbol, snapshot in fundamentals.items():
        snapshot_date = snapshot.get("as_of") if isinstance(snapshot, dict) else None
        try:
            age = (as_of - date.fromisoformat(str(snapshot_date)[:10])).days
        except (TypeError, ValueError):
            continue
        if 0 <= age <= 30:
            fresh_fundamentals.add(symbol)
    evidenced = fresh_fundamentals & {
        symbol for symbol, rows in bars_by_symbol.items() if rows
    }
    return relevant_delisted <= evidenced


def apply_point_in_time_classifications(
    *,
    as_of: date,
    companies: dict[str, dict[str, Any]],
    snapshots_by_symbol: dict[str, list[dict[str, Any]]],
    universe_symbols: set[str],
) -> tuple[dict[str, dict[str, Any]], float]:
    """Overlay the latest classification snapshot available at ``as_of``."""
    result = {symbol: dict(values) for symbol, values in companies.items()}
    covered = 0
    for symbol in universe_symbols:
        available = [
            row for row in snapshots_by_symbol.get(symbol, [])
            if str(row.get("snapshot_date", ""))[:10] <= as_of.isoformat()
        ]
        if not available:
            continue
        row = max(available, key=lambda item: str(item.get("snapshot_date", "")))
        company = result.setdefault(symbol, {})
        for key in ("name_zh", "industry", "exchange"):
            if row.get(key) is not None:
                company[key] = row[key]
        company["classification_as_of"] = str(row["snapshot_date"])[:10]
        if company.get("industry"):
            covered += 1
    coverage = covered / len(universe_symbols) * 100 if universe_symbols else 0.0
    return result, coverage


def _fundamental_yields(snapshot: dict[str, Any]) -> tuple[float | None, float | None]:
    """Return earnings/book yields separately so their scales cannot dominate."""
    pe = _number(snapshot.get("pe_ratio"), positive=True)
    pb = _number(snapshot.get("pb_ratio"), positive=True)
    return (1 / pe if pe is not None else None, 1 / pb if pb is not None else None)


def _project_bounded_simplex(
    values: dict[str, float], lower: dict[str, float], upper: dict[str, float],
) -> dict[str, float]:
    """Project weights onto sum=1 with per-factor lower/upper bounds."""
    low, high = -1.0, 1.0
    for _ in range(80):
        shift = (low + high) / 2
        total = sum(min(upper[key], max(lower[key], values[key] - shift)) for key in values)
        if total > 1:
            low = shift
        else:
            high = shift
    shift = (low + high) / 2
    projected = {
        key: min(upper[key], max(lower[key], values[key] - shift))
        for key in values
    }
    # Floating-point bisection is already very close; pin the residual to the
    # factor with the most remaining room so downstream coverage sums stay one.
    residual = 1 - sum(projected.values())
    if abs(residual) > 1e-12:
        key = max(
            projected,
            key=lambda item: (upper[item] - projected[item]) if residual > 0
            else (projected[item] - lower[item]),
        )
        projected[key] += residual
    return projected


def learn_walk_forward_weights(
    *, base_weights: dict[str, float], learning_history: list[dict[str, Any]],
    as_of: date, minimum_periods: int = 12, lookback_periods: int = 24,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Learn modest weights from IC labels already mature at ``as_of`` only."""
    mature = sorted(
        (row for row in learning_history
         if str(row.get("available_on", ""))[:10] <= as_of.isoformat()),
        key=lambda row: str(row.get("available_on", "")),
    )[-lookback_periods:]
    if len(mature) < minimum_periods:
        return dict(base_weights), {
            "source_period_count": len(mature),
            "fallback_reason": "insufficient_mature_labels",
            "mean_rank_ic": {},
        }
    samples = {
        factor: [
            float(row["rank_ic"][factor]) for row in mature
            if row.get("rank_ic", {}).get(factor) is not None
        ]
        for factor in FACTOR_NAMES
    }
    learned = {factor: values for factor, values in samples.items()
               if len(values) >= minimum_periods}
    if len(learned) < 3:
        return dict(base_weights), {
            "source_period_count": len(mature),
            "fallback_reason": "insufficient_factor_coverage",
            "mean_rank_ic": {
                factor: round(fmean(values), 6) for factor, values in learned.items()
            },
        }
    means = {factor: fmean(values) for factor, values in learned.items()}
    learned_weight = sum(base_weights[factor] for factor in learned)
    center = sum(base_weights[factor] * means[factor] for factor in learned) / learned_weight
    reliability = min(0.5, len(mature) / 48)
    raw = dict(base_weights)
    for factor in learned:
        target = base_weights[factor] * (1 + 3 * (means[factor] - center))
        raw[factor] = base_weights[factor] + reliability * (target - base_weights[factor])
    lower = {factor: weight * 0.5 for factor, weight in base_weights.items()}
    upper = {factor: weight * 1.5 for factor, weight in base_weights.items()}
    weights = _project_bounded_simplex(raw, lower, upper)
    return weights, {
        "source_period_count": len(mature),
        "fallback_reason": None,
        "mean_rank_ic": {factor: round(value, 6) for factor, value in means.items()},
        "reliability": round(reliability, 4),
        "constraints": "50%-150% of profile base weight",
    }


def build_factor_ranking(
    *,
    fundamentals: dict[str, dict[str, Any]],
    bars_by_symbol: dict[str, list[dict[str, Any]]],
    companies: dict[str, dict[str, Any]],
    as_of: date,
    profile: str = "balanced",
    limit: int = 50,
    historical: bool = False,
    universe_point_in_time: bool = False,
    classification_coverage_pct: float = 0.0,
    sector_neutral: bool = True,
    quality_by_symbol: dict[str, dict[str, Any]] | None = None,
    quality_availability_approximated: bool = False,
    weights_override: dict[str, float] | None = None,
    security_profiles: dict[str, dict[str, Any]] | None = None,
    security_master_coverage_pct: float = 0.0,
) -> dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"unknown factor profile: {profile}")
    weights = dict(weights_override or PROFILES[profile])
    if set(weights) != set(FACTOR_NAMES):
        raise ValueError("weights_override must contain every factor exactly once")
    parsed_weights = {factor: _number(value) for factor, value in weights.items()}
    if any(value is None or value < 0 for value in parsed_weights.values()):
        raise ValueError("factor weights must be finite and non-negative")
    weights = {factor: float(value) for factor, value in parsed_weights.items() if value is not None}
    total_weight = sum(weights.values())
    if total_weight <= 0:
        raise ValueError("factor weights must have positive total weight")
    weights = {factor: value / total_weight for factor, value in weights.items()}
    quality_by_symbol = quality_by_symbol or {}
    security_master_used = security_profiles is not None
    security_profiles = security_profiles or {}
    symbols = sorted(
        symbol for symbol in fundamentals
        if not security_profiles.get(symbol, {}).get("is_etf", is_etf(symbol))
        and (not companies or symbol in companies)
    )
    raw: dict[str, dict[str, Any]] = {}
    stale_fundamentals = 0
    stale_prices = 0
    future_inputs = 0
    for symbol in symbols:
        snapshot = fundamentals[symbol]
        snapshot_date = snapshot.get("as_of")
        try:
            age = (as_of - date.fromisoformat(str(snapshot_date)[:10])).days
        except (TypeError, ValueError):
            age = 9999
        if age < 0:
            future_inputs += 1
            continue
        if age > 30:
            stale_fundamentals += 1
            continue
        point_in_time_bars = [
            row for row in bars_by_symbol.get(symbol, [])
            if str(row.get("date") or row.get("time") or "")[:10] <= as_of.isoformat()
        ]
        price = _price_factors(point_in_time_bars)
        try:
            price_age = (as_of - date.fromisoformat(str(price["latest_session"])[:10])).days
        except (TypeError, ValueError):
            price_age = 9999
        if price_age > 10:
            stale_prices += 1
            continue
        earnings_yield, book_yield = _fundamental_yields(snapshot)
        quality = quality_by_symbol.get(symbol, {})
        if quality and str(quality.get("available_on", ""))[:10] > as_of.isoformat():
            future_inputs += 1
            quality = {}
        raw[symbol] = {
            "value": None,
            "earnings_yield": earnings_yield,
            "book_yield": book_yield,
            "quality": None,
            **{component: _number(quality.get(component)) for component in QUALITY_COMPONENTS},
            "momentum": price["momentum"],
            "low_volatility": price["low_volatility"],
            "income": _number(snapshot.get("dividend_yield")),
            "liquidity": price["liquidity"],
            "price": price["latest_close"],
            "price_session": price["latest_session"],
            "observations": price["observations"],
            "adjusted_observations": price["adjusted_observations"],
            "fundamentals_as_of": str(snapshot_date)[:10] if snapshot_date else None,
            "quality_period_end": quality.get("period_end"),
            "quality_available_on": quality.get("available_on"),
        }

    # Standardise value sub-factors independently before averaging. Raw book
    # yields are typically an order of magnitude larger than earnings yields;
    # averaging raw values would make P/B silently dominate P/E.
    earnings_z = winsorized_zscores({symbol: values["earnings_yield"] for symbol, values in raw.items()})
    book_z = winsorized_zscores({symbol: values["book_yield"] for symbol, values in raw.items()})
    for symbol, values in raw.items():
        components = [mapping[symbol] for mapping in (earnings_z, book_z) if symbol in mapping]
        values["value"] = fmean(components) if components else None

    quality_component_z = {
        component: winsorized_zscores({
            symbol: values[component] for symbol, values in raw.items()
        })
        for component in QUALITY_COMPONENTS
    }
    for symbol, values in raw.items():
        components = [
            mapping[symbol] for mapping in quality_component_z.values()
            if symbol in mapping
        ]
        values["quality"] = fmean(components) if len(components) >= 2 else None

    factor_z = {
        factor: winsorized_zscores({symbol: values[factor] for symbol, values in raw.items()})
        for factor in FACTOR_NAMES
    }
    candidates: list[dict[str, Any]] = []
    for symbol, values in raw.items():
        available = {factor: factor_z[factor][symbol]
                     for factor in FACTOR_NAMES if symbol in factor_z[factor]}
        coverage = sum(weights[factor] for factor in available)
        if coverage + 1e-12 < MIN_WEIGHT_COVERAGE:
            continue
        composite = sum(weights[factor] * z for factor, z in available.items()) / coverage
        company = companies.get(symbol, {})
        candidates.append({
            "symbol": symbol,
            "name_zh": company.get("name_zh"),
            "industry": company.get("industry"),
            "price": values["price"],
            "price_session": values["price_session"],
            "fundamentals_as_of": values["fundamentals_as_of"],
            "quality_period_end": values["quality_period_end"],
            "quality_available_on": values["quality_available_on"],
            "composite_z": composite,
            "raw_composite_z": composite,
            "sector_adjustment": None,
            "factor_coverage": round(coverage, 4),
            "missing_factors": [factor for factor in FACTOR_NAMES if factor not in available],
            "factors": {
                factor: {
                    "raw": round(float(values[factor]), 6) if values[factor] is not None else None,
                    "z": round(available[factor], 4) if factor in available else None,
                }
                for factor in FACTOR_NAMES
            },
        })

    sector_coverage = 0.0
    sector_neutral_applied = False
    if sector_neutral and candidates:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for candidate in candidates:
            industry = candidate.get("industry")
            if industry:
                groups[str(industry)].append(candidate)
        eligible_groups = {
            industry: rows for industry, rows in groups.items()
            if len(rows) >= MIN_SECTOR_GROUP_SIZE
        }
        eligible_symbols = {
            row["symbol"] for rows in eligible_groups.values() for row in rows
        }
        sector_coverage = len(eligible_symbols) / len(candidates) * 100
        if sector_coverage >= MIN_SECTOR_COVERAGE * 100:
            residuals: dict[str, float] = {}
            group_means: dict[str, float] = {}
            for industry, rows in eligible_groups.items():
                group_mean = fmean(float(row["raw_composite_z"]) for row in rows)
                group_means[industry] = group_mean
                for row in rows:
                    residuals[row["symbol"]] = float(row["raw_composite_z"]) - group_mean
            residual_z = winsorized_zscores(residuals)
            candidates = [row for row in candidates if row["symbol"] in residual_z]
            for candidate in candidates:
                candidate["sector_adjustment"] = round(
                    group_means[str(candidate["industry"])], 4,
                )
                candidate["composite_z"] = residual_z[candidate["symbol"]]
            sector_neutral_applied = True

    candidates.sort(key=lambda row: (-row["composite_z"], row["symbol"]))
    count = len(candidates)
    for index, candidate in enumerate(candidates):
        candidate["rank"] = index + 1
        candidate["score"] = round(100 * (count - index) / count, 2) if count else 0
        candidate["composite_z"] = round(candidate["composite_z"], 4)
        candidate["raw_composite_z"] = round(candidate["raw_composite_z"], 4)

    price_factor_coverage = (
        sum(raw[symbol]["momentum"] is not None for symbol in raw) / len(raw) * 100
        if raw else 0.0
    )
    quality_factor_coverage = (
        sum(raw[symbol]["quality"] is not None for symbol in raw) / len(raw) * 100
        if raw else 0.0
    )
    price_observations = sum(int(values["observations"]) for values in raw.values())
    adjusted_observations = sum(int(values["adjusted_observations"]) for values in raw.values())
    adjusted_coverage = adjusted_observations / price_observations * 100 if price_observations else 0.0
    flags: list[str] = []
    if adjusted_coverage == 0:
        flags.append("unadjusted_price_history")
    elif adjusted_coverage < 95:
        flags.append("partial_adjusted_price_history")
    if historical:
        if not universe_point_in_time:
            flags.append("survivorship_bias")
        if classification_coverage_pct < 95:
            flags.append("sector_classification_not_point_in_time")
    if sector_neutral and not sector_neutral_applied:
        flags.append("sector_neutralization_unavailable")
    elif sector_neutral and sector_coverage < 95:
        flags.append("partial_sector_neutralization")
    if stale_fundamentals:
        flags.append("stale_fundamentals_excluded")
    if stale_prices:
        flags.append("stale_price_history_excluded")
    if future_inputs:
        flags.append("future_dated_inputs_excluded")
    if price_factor_coverage < 60:
        flags.append("low_momentum_coverage")
    if quality_factor_coverage < 60:
        flags.append("low_quality_factor_coverage")
    if quality_availability_approximated and quality_factor_coverage > 0:
        flags.append("financial_statement_availability_approximated")
    if security_master_used and security_master_coverage_pct < 95:
        flags.append("security_master_fallback")
    evidence_complete = adjusted_coverage >= 95 and (
        not historical or universe_point_in_time
    ) and (not historical or classification_coverage_pct >= 95) and (
        not sector_neutral or sector_neutral_applied
    ) and quality_factor_coverage >= 60
    if security_master_used:
        evidence_complete = evidence_complete and security_master_coverage_pct >= 95
    status = (
        "unavailable" if not candidates
        else "good" if len(candidates) >= 30 and price_factor_coverage >= 60
        and evidence_complete
        else "degraded"
    )
    result = {
        "market": "TW",
        "as_of": as_of.isoformat(),
        "profile": profile,
        "methodology_version": METHOD_VERSION,
        "weights": weights,
        "candidates": candidates[:limit],
        "quality": {
            "status": status,
            "flags": flags,
            "universe_size": len(symbols),
            "eligible_count": count,
            "returned_count": min(limit, count),
            "momentum_coverage_pct": round(price_factor_coverage, 1),
            "quality_factor_coverage_pct": round(quality_factor_coverage, 1),
            "adjusted_price_coverage_pct": round(adjusted_coverage, 1),
            "classification_coverage_pct": round(classification_coverage_pct, 1),
            **({
                "security_master_coverage_pct": round(security_master_coverage_pct, 1),
            } if security_master_used else {}),
            "sector_coverage_pct": round(sector_coverage, 1),
            "sector_neutral_applied": sector_neutral_applied,
            "stale_fundamentals_excluded": stale_fundamentals,
            "stale_price_history_excluded": stale_prices,
            "future_dated_inputs_excluded": future_inputs,
            "sources": [
                "fundamentals_snapshots", "ohlcv_daily", "finmind.tw_stock_price_adj",
                "finmind.tw_stock_info", "finmind.tw_delisting",
                "tw_company_classification_snapshots",
                *(["tw_security_master_versions"] if security_master_used else []),
                *(["finmind.tw_income_statement", "finmind.tw_balance_sheet",
                   "finmind.tw_cash_flow"] if quality_factor_coverage > 0 else []),
            ],
        },
        "methodology": {
            "value": "mean of separately standardised positive earnings yield (1/PE) and book yield (1/PB)",
            "quality": (
                "mean of separately standardised operating margin, return on assets, "
                "operating-cash-flow return on assets, and negative debt ratio; at least "
                "two components required"
            ),
            "momentum": "126-session corporate-action-adjusted return ending 21 sessions before as_of",
            "low_volatility": "negative annualised volatility of 63 adjusted daily log returns",
            "income": "reported dividend yield percentage",
            "liquidity": "log of 20-session average close × volume",
            "normalisation": "5/95 winsorised cross-sectional population z-score",
            "sector_neutralisation": (
                "subtract industry mean composite, then winsorised z-score; "
                "industries require at least 2 eligible stocks"
                if sector_neutral else "disabled by request"
            ),
            "missing_data": "renormalise available profile weights only at >=60% coverage",
            "model": "deterministic ranking; not machine learning and not investment advice",
        },
        "sector_neutral": sector_neutral,
    }
    return result


async def _load_inputs(
    db: AsyncSession, *, as_of: date, start: date,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    latest = (
        select(
            FundamentalsSnapshot.symbol.label("symbol"),
            func.max(FundamentalsSnapshot.as_of).label("latest_as_of"),
        )
        .where(FundamentalsSnapshot.market == "TW", FundamentalsSnapshot.as_of <= as_of)
        .group_by(FundamentalsSnapshot.symbol)
        .subquery()
    )
    fundamental_stmt = (
        select(FundamentalsSnapshot)
        .join(latest, and_(
            FundamentalsSnapshot.symbol == latest.c.symbol,
            FundamentalsSnapshot.as_of == latest.c.latest_as_of,
        ))
        .where(FundamentalsSnapshot.market == "TW")
    )
    fundamental_rows = (await db.scalars(fundamental_stmt)).all()
    fundamentals = {
        row.symbol: {
            "as_of": row.as_of.isoformat(),
            "pe_ratio": _number(row.pe_ratio),
            "pb_ratio": _number(row.pb_ratio),
            "dividend_yield": _number(row.dividend_yield),
        }
        for row in fundamental_rows
    }

    company_rows = (await db.scalars(select(TwCompanyInfo))).all()
    companies = {
        row.symbol: {"name_zh": row.name_zh, "industry": row.industry, "exchange": row.exchange}
        for row in company_rows
    }

    bar_stmt = (
        select(OhlcvDaily)
        .where(
            OhlcvDaily.market == "TW",
            OhlcvDaily.ts >= start,
            OhlcvDaily.ts <= as_of,
            OhlcvDaily.close.isnot(None),
        )
        .order_by(OhlcvDaily.symbol.asc(), OhlcvDaily.ts.asc())
    )
    bar_rows = (await db.scalars(bar_stmt)).all()
    bars_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in bar_rows:
        bars_by_symbol[row.symbol].append({
            "date": row.ts.isoformat(),
            "close": _number(row.close),
            "volume": int(row.volume) if row.volume is not None else None,
        })
    return fundamentals, dict(bars_by_symbol), companies


async def _load_classification_history(
    db: AsyncSession, *, end: date,
) -> dict[str, list[dict[str, Any]]]:
    rows = (await db.scalars(
        select(TwCompanyClassificationSnapshot).where(
            TwCompanyClassificationSnapshot.snapshot_date <= end,
        ).order_by(
            TwCompanyClassificationSnapshot.symbol,
            TwCompanyClassificationSnapshot.snapshot_date,
        )
    )).all()
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        result[row.symbol].append({
            "snapshot_date": row.snapshot_date.isoformat(),
            "exchange": row.exchange,
            "industry": row.industry,
            "name_zh": row.name_zh,
        })
    return dict(result)


def _statement_value(row: Any, attribute: str, aliases: tuple[str, ...]) -> float | None:
    value = _number(getattr(row, attribute, None))
    if value is not None:
        return value
    raw = getattr(row, "raw", None) or {}
    for alias in aliases:
        value = _number(raw.get(alias))
        if value is not None:
            return value
    return None


async def _load_quality_statement_history(
    *, end: date, start: date | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """Load quarterly quality inputs with conservative availability dates."""
    try:
        from finmind.db.session import FinmindAsyncSessionLocal
        from finmind.models.fundamental import TwBalanceSheet, TwCashFlow, TwIncomeStatement
        from services.tw_statement_availability import statement_available_on
    except ImportError:
        return {}, False

    try:
        lower_bound = (start or end) - timedelta(days=220)
        async with FinmindAsyncSessionLocal() as db:
            income_rows = (await db.scalars(select(TwIncomeStatement).where(
                TwIncomeStatement.period_end.isnot(None),
                TwIncomeStatement.period_end >= lower_bound,
                TwIncomeStatement.period_end <= end,
            ))).all()
            balance_rows = (await db.scalars(select(TwBalanceSheet).where(
                TwBalanceSheet.period_end.isnot(None),
                TwBalanceSheet.period_end >= lower_bound,
                TwBalanceSheet.period_end <= end,
            ))).all()
            cash_rows = (await db.scalars(select(TwCashFlow).where(
                TwCashFlow.period_end.isnot(None),
                TwCashFlow.period_end >= lower_bound,
                TwCashFlow.period_end <= end,
            ))).all()
    except Exception:
        return {}, False

    income = {(row.symbol, row.period): row for row in income_rows}
    balance = {(row.symbol, row.period): row for row in balance_rows}
    cash = {(row.symbol, row.period): row for row in cash_rows}
    history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key in sorted(set(income) & set(balance)):
        income_row, balance_row = income[key], balance[key]
        period_end = income_row.period_end or balance_row.period_end
        if period_end is None:
            continue
        revenue = _statement_value(
            income_row, "revenue", ("Revenue", "OperatingRevenue", "NetSales", "TotalRevenue"),
        )
        operating_income = _statement_value(
            income_row, "operating_income",
            ("OperatingIncome", "OperatingProfit", "IncomeFromOperations"),
        )
        net_income = _statement_value(
            income_row, "net_income",
            ("NetIncome", "NetIncomeAttributableToOwnersOfParent", "IncomeAfterTax"),
        )
        assets = _statement_value(balance_row, "total_assets", ("TotalAssets", "Assets"))
        liabilities = _statement_value(
            balance_row, "total_liabilities", ("TotalLiabilities", "Liabilities"),
        )
        cash_row = cash.get(key)
        operating_cash_flow = _statement_value(
            cash_row, "operating_cash_flow",
            ("CashFlowsFromOperatingActivities", "NetCashProvidedByOperatingActivities"),
        ) if cash_row is not None else None

        def ratio(numerator: float | None, denominator: float | None) -> float | None:
            if numerator is None or denominator in (None, 0):
                return None
            return numerator / float(denominator)

        snapshot = {
            "period": income_row.period,
            "period_end": period_end.isoformat(),
            "available_on": statement_available_on(period_end).isoformat(),
            "operating_margin": ratio(operating_income, revenue),
            "return_on_assets": ratio(net_income, assets),
            "cash_return_on_assets": ratio(operating_cash_flow, assets),
            "balance_strength": -value if (value := ratio(liabilities, assets)) is not None else None,
        }
        if sum(snapshot[component] is not None for component in QUALITY_COMPONENTS) >= 2:
            history[income_row.symbol].append(snapshot)
    for rows in history.values():
        rows.sort(key=lambda row: (row["available_on"], row["period_end"]))
    return dict(history), bool(income_rows and balance_rows)


def point_in_time_quality(
    *, history: dict[str, list[dict[str, Any]]], as_of: date,
) -> dict[str, dict[str, Any]]:
    """Select the latest conservatively available, non-stale quality snapshot."""
    result: dict[str, dict[str, Any]] = {}
    for symbol, rows in history.items():
        available = [row for row in rows if row["available_on"] <= as_of.isoformat()]
        if not available:
            continue
        latest = available[-1]
        try:
            age = (as_of - date.fromisoformat(latest["period_end"])).days
        except (TypeError, ValueError):
            continue
        if age <= 220:
            result[symbol] = latest
    return result


async def _load_research_sidecars(
    *, start: date, end: date,
) -> tuple[
    dict[str, dict[str, float]],
    dict[str, dict[str, Any]],
    dict[str, date],
    bool,
]:
    """Read adjusted prices and lifecycle dimensions from FinMind storage.

    The subsystem may be disabled or temporarily unavailable on small
    deployments, so this is an optional evidence tier. Each query gets an
    independent session: a missing table cannot poison the other sidecars.
    """
    try:
        from finmind.db.session import FinmindAsyncSessionLocal
        from finmind.models.master import TwDelisting, TwStockInfo
        from finmind.models.technical import TwStockPriceAdj
    except ImportError:
        return {}, {}, {}, False

    adjusted: dict[str, dict[str, float]] = defaultdict(dict)
    try:
        async with FinmindAsyncSessionLocal() as db:
            rows = (await db.scalars(
                select(TwStockPriceAdj).where(
                    TwStockPriceAdj.ts >= start,
                    TwStockPriceAdj.ts <= end,
                    TwStockPriceAdj.adj_close.isnot(None),
                ).order_by(TwStockPriceAdj.symbol, TwStockPriceAdj.ts)
            )).all()
            for row in rows:
                value = _number(row.adj_close, positive=True)
                if value is not None:
                    adjusted[row.symbol][row.ts.isoformat()] = value
    except Exception:
        adjusted = defaultdict(dict)

    stock_info: dict[str, dict[str, Any]] = {}
    stock_info_available = False
    try:
        async with FinmindAsyncSessionLocal() as db:
            rows = (await db.scalars(
                select(TwStockInfo).where(TwStockInfo.is_warrant.is_(False))
            )).all()
            stock_info_available = bool(rows)
            for row in rows:
                stock_info[row.symbol] = {
                    "name_zh": row.name_zh,
                    "industry": row.industry_category,
                    "exchange": row.market,
                    "listed_at": row.listed_at,
                }
    except Exception:
        pass

    delistings: dict[str, date] = {}
    delistings_available = False
    try:
        async with FinmindAsyncSessionLocal() as db:
            rows = (await db.scalars(select(TwDelisting))).all()
            delistings_available = bool(rows)
            delistings = {row.symbol: row.delisted_at for row in rows}
    except Exception:
        pass

    return dict(adjusted), stock_info, delistings, (
        stock_info_available and delistings_available
    )


async def get_factor_ranking(
    *, as_of: date | None = None, profile: str = "balanced", limit: int = 50,
    sector_neutral: bool = True,
    weights_override: dict[str, float] | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    anchor = as_of or date.today()
    cache_key = key_factor_ranking_tw(
        anchor.isoformat(), profile, limit, sector_neutral,
        model_id,
    )
    cached = await cache_get_json(cache_key)
    if cached is not None:
        FACTOR_ANALYSIS_TOTAL.labels(kind="ranking", outcome=cached["quality"]["status"]).inc()
        return cached
    async with AsyncSessionLocal() as db:
        fundamentals, bars, companies = await _load_inputs(
            db, as_of=anchor, start=anchor - timedelta(days=550),
        )
        classifications = await _load_classification_history(db, end=anchor)
        from services.tw_security_master_service import resolve_security_profiles

        security_profiles = await resolve_security_profiles(
            db, set(fundamentals), as_of=anchor,
        )
    adjusted, stock_info, delistings, lifecycle_catalog_ready = await _load_research_sidecars(
        start=anchor - timedelta(days=550), end=anchor,
    )
    quality_history, quality_history_ready = await _load_quality_statement_history(
        start=anchor, end=anchor,
    )
    bars = merge_adjusted_prices(bars, adjusted)
    companies = point_in_time_companies(
        as_of=anchor, current=companies, stock_info=stock_info,
        delistings=delistings, bars_by_symbol=bars,
    )
    companies, classification_coverage = apply_point_in_time_classifications(
        as_of=anchor, companies=companies,
        snapshots_by_symbol=classifications,
        universe_symbols=set(fundamentals) & set(bars),
    )
    lifecycle_ready = point_in_time_universe_ready(
        catalog_ready=lifecycle_catalog_ready, as_of=anchor,
        delistings=delistings, fundamentals=fundamentals, bars_by_symbol=bars,
    )
    master_coverage = (
        sum(not row.get("fallback", True) for row in security_profiles.values())
        / max(len(security_profiles), 1) * 100
    )
    result = build_factor_ranking(
        fundamentals=fundamentals, bars_by_symbol=bars, companies=companies,
        as_of=anchor, profile=profile, limit=limit, historical=as_of is not None,
        universe_point_in_time=lifecycle_ready,
        classification_coverage_pct=classification_coverage,
        sector_neutral=sector_neutral,
        quality_by_symbol=point_in_time_quality(history=quality_history, as_of=anchor),
        quality_availability_approximated=quality_history_ready,
        weights_override=weights_override,
        security_profiles=security_profiles,
        security_master_coverage_pct=master_coverage,
    )
    result["weight_source"] = "champion" if model_id else "profile"
    result["model_id"] = model_id
    result["methodology"]["weight_source"] = (
        "user-scoped promoted champion model weights"
        if model_id else "static profile weights"
    )
    FACTOR_ANALYSIS_TOTAL.labels(kind="ranking", outcome=result["quality"]["status"]).inc()
    await cache_set_json(cache_key, result, TTL_FACTOR_RANKING)
    return result


def _is_suspended(
    symbol: str, session: date,
    suspensions: dict[str, list[tuple[date, date | None]]],
) -> bool:
    return any(
        started <= session and (resumed is None or session < resumed)
        for started, resumed in suspensions.get(symbol, [])
    )


def _limit_locked(
    row: dict[str, Any], limits: dict[str, float | None], *, side: str,
) -> bool:
    raw_close = _number(row.get("raw_close", row.get("close")), positive=True)
    threshold = _number(
        limits.get("upper_limit" if side == "buy" else "lower_limit"),
        positive=True,
    )
    if raw_close is None or threshold is None:
        return False
    tolerance = max(abs(threshold) * 0.0005, 0.01)
    return raw_close >= threshold - tolerance if side == "buy" else raw_close <= threshold + tolerance


def simulate_forward_trade(
    *,
    symbol: str,
    bars: list[dict[str, Any]],
    market_sessions: list[date],
    anchor: date,
    holding_sessions: int,
    target_notional_twd: float,
    max_participation_rate: float,
    impact_coefficient_bps: float,
    price_limits: dict[str, dict[str, dict[str, float | None]]],
    suspensions: dict[str, list[tuple[date, date | None]]],
) -> dict[str, Any] | None:
    """Simulate a capacity-constrained long trade on market-session dates."""
    session_index = {session: index for index, session in enumerate(market_sessions)}
    anchor_index = session_index.get(anchor)
    if anchor_index is None:
        return None
    rows = {
        date.fromisoformat(str(row.get("date", ""))[:10]): row
        for row in bars
        if row.get("date") and _number(row.get("close"), positive=True) is not None
    }

    def find_fill(start_index: int, *, side: str) -> tuple[int, dict[str, Any]] | None:
        for delay in range(MAX_EXECUTION_DEFERRAL_SESSIONS + 1):
            index = start_index + delay
            if index >= len(market_sessions):
                break
            session = market_sessions[index]
            row = rows.get(session)
            if row is None or _is_suspended(symbol, session, suspensions):
                continue
            limits = price_limits.get(symbol, {}).get(session.isoformat(), {})
            if _limit_locked(row, limits, side=side):
                continue
            return index, row
        return None

    entry = find_fill(anchor_index + 1, side="buy")
    if entry is None:
        return {"executed": False, "blocked_side": "entry"}
    entry_index, entry_row = entry
    exit_target = entry_index + holding_sessions
    exit_fill = find_fill(exit_target, side="sell")
    if exit_fill is None:
        return {"executed": False, "blocked_side": "exit"}
    exit_index, exit_row = exit_fill

    entry_price = float(entry_row["close"])
    exit_price = float(exit_row["close"])
    entry_raw = _number(entry_row.get("raw_close", entry_row.get("close")), positive=True)
    exit_raw = _number(exit_row.get("raw_close", exit_row.get("close")), positive=True)
    entry_volume = _number(entry_row.get("volume"), positive=True)
    exit_volume = _number(exit_row.get("volume"), positive=True)
    if None in (entry_raw, exit_raw, entry_volume, exit_volume):
        return {"executed": False, "blocked_side": "capacity"}
    entry_value = float(entry_raw) * float(entry_volume)
    exit_value = float(exit_raw) * float(exit_volume)
    capacity = min(entry_value, exit_value) * max_participation_rate
    fill_ratio = min(1.0, capacity / target_notional_twd) if target_notional_twd > 0 else 0.0
    if fill_ratio <= 0:
        return {"executed": False, "blocked_side": "capacity"}
    filled_notional = target_notional_twd * fill_ratio
    entry_participation = filled_notional / entry_value
    exit_participation = filled_notional / exit_value
    impact_bps = impact_coefficient_bps * (
        math.sqrt(entry_participation) + math.sqrt(exit_participation)
    )
    gross_return = (exit_price / entry_price - 1) * fill_ratio
    return {
        "executed": True,
        "gross_return": gross_return,
        "fill_ratio": fill_ratio,
        "impact_cost": fill_ratio * impact_bps / 10_000,
        "entry_session": market_sessions[entry_index].isoformat(),
        "exit_session": market_sessions[exit_index].isoformat(),
        "entry_delay_sessions": entry_index - (anchor_index + 1),
        "exit_delay_sessions": exit_index - exit_target,
        "capacity_limited": fill_ratio < 0.999999,
    }


def forward_asset_return(
    *, bars: list[dict[str, Any]], market_sessions: list[date],
    anchor: date, holding_sessions: int,
) -> float | None:
    """Return an asset's next-session-to-exit return without future imputation."""
    try:
        anchor_index = market_sessions.index(anchor)
        entry_session = market_sessions[anchor_index + 1]
        exit_session = market_sessions[anchor_index + 1 + holding_sessions]
    except (ValueError, IndexError):
        return None
    closes = {
        date.fromisoformat(str(row.get("date", ""))[:10]): _number(row.get("close"), positive=True)
        for row in bars if row.get("date")
    }
    entry, exit_value = closes.get(entry_session), closes.get(exit_session)
    if entry is None or exit_value is None:
        return None
    return exit_value / entry - 1


def benchmark_forward_return(
    *, bars: list[dict[str, Any]], market_sessions: list[date],
    anchor: date, holding_sessions: int,
) -> float | None:
    return forward_asset_return(
        bars=bars, market_sessions=market_sessions, anchor=anchor,
        holding_sessions=holding_sessions,
    )


def _average_ranks(values: list[float]) -> list[float]:
    """Return one-based average ranks, including deterministic tie handling."""
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(ordered):
        end = cursor + 1
        while end < len(ordered) and ordered[end][1] == ordered[cursor][1]:
            end += 1
        average_rank = (cursor + 1 + end) / 2
        for index in range(cursor, end):
            ranks[ordered[index][0]] = average_rank
        cursor = end
    return ranks


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean, right_mean = fmean(left), fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True))
    left_scale = math.sqrt(sum((x - left_mean) ** 2 for x in left))
    right_scale = math.sqrt(sum((y - right_mean) ** 2 for y in right))
    if left_scale == 0 or right_scale == 0:
        return None
    return numerator / (left_scale * right_scale)


def spearman_rank_correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    return _pearson(_average_ranks(left), _average_ranks(right))


def diagnose_factor_cross_section(
    *, candidates: list[dict[str, Any]], forward_returns: dict[str, float],
) -> dict[str, Any]:
    """Compute cost-free signal diagnostics for one point-in-time cross-section."""
    eligible = [row for row in candidates if row["symbol"] in forward_returns]
    rank_ic: dict[str, float | None] = {}
    signal_values: dict[str, dict[str, float]] = {name: {} for name in DIAGNOSTIC_SIGNALS}
    for row in eligible:
        symbol = row["symbol"]
        signal_values["composite"][symbol] = float(row["composite_z"])
        for factor in FACTOR_NAMES:
            value = _number(row.get("factors", {}).get(factor, {}).get("z"))
            if value is not None:
                signal_values[factor][symbol] = value
    for signal, values in signal_values.items():
        symbols = sorted(set(values) & set(forward_returns))
        correlation = spearman_rank_correlation(
            [values[symbol] for symbol in symbols],
            [forward_returns[symbol] for symbol in symbols],
        ) if len(symbols) >= 8 else None
        rank_ic[signal] = round(correlation, 6) if correlation is not None else None

    ordered = sorted(
        eligible, key=lambda row: (float(row["composite_z"]), row["symbol"]),
    )
    quintiles: list[list[float]] = [[] for _ in range(5)]
    if len(ordered) >= 10:
        for index, row in enumerate(ordered):
            bucket = min(4, index * 5 // len(ordered))
            quintiles[bucket].append(forward_returns[row["symbol"]])
    quintile_returns = [fmean(values) if values else None for values in quintiles]
    spread = (
        quintile_returns[4] - quintile_returns[0]
        if quintile_returns[4] is not None and quintile_returns[0] is not None else None
    )

    correlations: dict[str, dict[str, float | None]] = {}
    for left in FACTOR_NAMES:
        correlations[left] = {}
        for right in FACTOR_NAMES:
            symbols = sorted(set(signal_values[left]) & set(signal_values[right]))
            value = spearman_rank_correlation(
                [signal_values[left][symbol] for symbol in symbols],
                [signal_values[right][symbol] for symbol in symbols],
            ) if len(symbols) >= 8 else None
            correlations[left][right] = round(value, 6) if value is not None else None

    return {
        "observation_count": len(eligible),
        "rank_ic": rank_ic,
        "quintile_returns_pct": [round(value * 100, 4) if value is not None else None
                                 for value in quintile_returns],
        "top_bottom_spread_pct": round(spread * 100, 4) if spread is not None else None,
        "factor_correlations": correlations,
    }


def aggregate_factor_diagnostics(
    *, periods: list[dict[str, Any]], correlation_samples: list[dict[str, dict[str, float | None]]],
    holding_sessions: int,
) -> tuple[
    dict[str, dict[str, float | int | bool | None]],
    dict[str, dict[str, float | None]],
    dict[str, float | int | list[float | None] | None],
]:
    """Aggregate ICs and apply Holm correction across reported signals."""
    diagnostics: dict[str, dict[str, float | int | bool | None]] = {}
    raw_p_values: dict[str, float] = {}
    for signal in DIAGNOSTIC_SIGNALS:
        values = [
            float(row["rank_ic"][signal]) for row in periods
            if row.get("rank_ic", {}).get(signal) is not None
        ]
        t_stat: float | None = None
        p_value: float | None = None
        ic_ir: float | None = None
        if len(values) >= 3 and stdev(values) > 0:
            t_stat = fmean(values) / (stdev(values) / math.sqrt(len(values)))
            p_value = float(2 * student_t.sf(abs(t_stat), df=len(values) - 1))
            ic_ir = fmean(values) / stdev(values) * math.sqrt(252 / holding_sessions)
            raw_p_values[signal] = p_value
        diagnostics[signal] = {
            "period_count": len(values),
            "average_rank_ic": round(fmean(values), 4) if values else None,
            "median_rank_ic": round(median(values), 4) if values else None,
            "positive_ic_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100, 1)
            if values else None,
            "ic_t_stat": round(t_stat, 3) if t_stat is not None else None,
            "p_value": round(p_value, 6) if p_value is not None else None,
            "annualized_ic_ir": round(ic_ir, 3) if ic_ir is not None else None,
            "holm_adjusted_p_value": None,
            "significant_after_holm_5pct": False,
        }
    running_max = 0.0
    tests = len(raw_p_values)
    for rank, (signal, p_value) in enumerate(sorted(raw_p_values.items(), key=lambda item: item[1])):
        adjusted = min(1.0, max(running_max, (tests - rank) * p_value))
        running_max = adjusted
        diagnostics[signal]["holm_adjusted_p_value"] = round(adjusted, 6)
        diagnostics[signal]["significant_after_holm_5pct"] = adjusted < 0.05

    matrix: dict[str, dict[str, float | None]] = {}
    for left in FACTOR_NAMES:
        matrix[left] = {}
        for right in FACTOR_NAMES:
            values = [
                float(sample[left][right]) for sample in correlation_samples
                if sample.get(left, {}).get(right) is not None
            ]
            matrix[left][right] = round(fmean(values), 4) if values else None

    complete_quintiles = [
        row["quintile_returns_pct"] for row in periods
        if len(row.get("quintile_returns_pct", [])) == 5
        and all(value is not None for value in row["quintile_returns_pct"])
    ]
    spreads = [
        float(row["top_bottom_spread_pct"]) for row in periods
        if row.get("top_bottom_spread_pct") is not None
    ]
    quantiles: dict[str, float | int | list[float | None] | None] = {
        "period_count": len(complete_quintiles),
        "average_returns_pct": [
            round(fmean(float(row[index]) for row in complete_quintiles), 4)
            if complete_quintiles else None for index in range(5)
        ],
        "average_top_bottom_spread_pct": round(fmean(spreads), 4) if spreads else None,
        "positive_spread_rate_pct": round(
            sum(value > 0 for value in spreads) / len(spreads) * 100, 1,
        ) if spreads else None,
    }
    return diagnostics, matrix, quantiles


def benchmark_volatility(
    *, bars: list[dict[str, Any]], anchor: date, lookback_sessions: int = 63,
) -> float | None:
    """Annualised benchmark volatility using only closes known at the anchor."""
    known = sorted(
        (date.fromisoformat(str(row["date"])[:10]), value)
        for row in bars
        if row.get("date") and str(row["date"])[:10] <= anchor.isoformat()
        and (value := _number(row.get("close"), positive=True)) is not None
    )[-(lookback_sessions + 1):]
    if len(known) < 21:
        return None
    returns = [math.log(known[index][1] / known[index - 1][1]) for index in range(1, len(known))]
    return pstdev(returns) * math.sqrt(252) if len(returns) >= 2 else None


def excess_return_statistics(values: list[float]) -> dict[str, float | None]:
    """Deterministic descriptive inference for non-overlapping period returns."""
    if not values:
        return {
            "excess_return_t_stat": None,
            "excess_return_ci_low_pct": None,
            "excess_return_ci_high_pct": None,
        }
    t_stat: float | None = None
    if len(values) >= 2:
        sample_std = stdev(values)
        if sample_std > 0:
            t_stat = fmean(values) / (sample_std / math.sqrt(len(values)))
    ci_low: float | None = None
    ci_high: float | None = None
    if len(values) >= 5:
        rng = Random(812033727)
        boot_means = sorted(
            fmean(values[rng.randrange(len(values))] for _ in values)
            for _ in range(2_000)
        )
        ci_low = _percentile(boot_means, 0.025) * 100
        ci_high = _percentile(boot_means, 0.975) * 100
    return {
        "excess_return_t_stat": round(t_stat, 3) if t_stat is not None else None,
        "excess_return_ci_low_pct": round(ci_low, 3) if ci_low is not None else None,
        "excess_return_ci_high_pct": round(ci_high, 3) if ci_high is not None else None,
    }


def build_regime_analysis(periods: list[dict[str, Any]]) -> dict[str, dict[str, float | int | None]]:
    """Summarise strategy behaviour in benchmark direction and volatility regimes."""
    volatility_values = [
        float(row["benchmark_volatility_pct"])
        for row in periods if row.get("benchmark_volatility_pct") is not None
    ]
    volatility_cutoff = median(volatility_values) if volatility_values else None
    groups: dict[str, list[dict[str, Any]]] = {
        "bull": [], "bear": [], "high_volatility": [], "low_volatility": [],
    }
    for row in periods:
        direction = "bull" if float(row["benchmark_return_pct"]) >= 0 else "bear"
        row["market_regime"] = direction
        groups[direction].append(row)
        value = row.get("benchmark_volatility_pct")
        if value is not None and volatility_cutoff is not None:
            groups["high_volatility" if float(value) >= volatility_cutoff else "low_volatility"].append(row)

    result: dict[str, dict[str, float | int | None]] = {}
    for name, rows in groups.items():
        result[name] = {
            "period_count": len(rows),
            "average_return_pct": round(fmean(float(row["net_return_pct"]) for row in rows), 3) if rows else None,
            "average_excess_return_pct": round(
                fmean(float(row["excess_return_pct"]) for row in rows), 3,
            ) if rows else None,
            "positive_excess_rate_pct": round(
                sum(float(row["excess_return_pct"]) > 0 for row in rows) / len(rows) * 100, 1,
            ) if rows else None,
        }
    result["volatility_threshold"] = {
        "annualized_volatility_pct": round(volatility_cutoff, 3) if volatility_cutoff is not None else None,
    }
    return result


async def _load_execution_sidecars(
    *, start: date, end: date,
) -> tuple[
    dict[str, dict[str, dict[str, float | None]]],
    dict[str, list[tuple[date, date | None]]],
    bool,
    bool,
]:
    try:
        from finmind.db.session import FinmindAsyncSessionLocal
        from finmind.models.technical import TwPriceLimitDaily, TwSuspended
    except ImportError:
        return {}, {}, False, False

    price_limits: dict[str, dict[str, dict[str, float | None]]] = defaultdict(dict)
    limits_available = False
    try:
        async with FinmindAsyncSessionLocal() as db:
            rows = (await db.scalars(select(TwPriceLimitDaily).where(
                TwPriceLimitDaily.ts >= start, TwPriceLimitDaily.ts <= end,
            ))).all()
            limits_available = bool(rows)
            for row in rows:
                price_limits[row.symbol][row.ts.isoformat()] = {
                    "upper_limit": _number(row.upper_limit),
                    "lower_limit": _number(row.lower_limit),
                }
    except Exception:
        pass

    suspensions: dict[str, list[tuple[date, date | None]]] = defaultdict(list)
    suspensions_available = False
    try:
        async with FinmindAsyncSessionLocal() as db:
            rows = (await db.scalars(select(TwSuspended).where(
                TwSuspended.suspended_at <= end,
            ))).all()
            suspensions_available = bool(rows)
            for row in rows:
                if row.resumed_at is None or row.resumed_at >= start:
                    suspensions[row.symbol].append((row.suspended_at, row.resumed_at))
    except Exception:
        pass
    return dict(price_limits), dict(suspensions), limits_available, suspensions_available


async def validate_factor_ranking(
    *, start_date: date, end_date: date, profile: str = "balanced",
    top_n: int = 20, holding_sessions: int = 21, transaction_cost_bps: float = 20,
    sector_neutral: bool = True,
    portfolio_notional_twd: float = 10_000_000,
    max_participation_rate: float = 0.05,
    impact_coefficient_bps: float = 10,
    benchmark: str = "taiex_total_return",
    weight_mode: str = "walk_forward",
) -> dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"unknown factor profile: {profile}")
    if start_date >= end_date:
        raise ValueError("start_date must be before end_date")
    if portfolio_notional_twd <= 0:
        raise ValueError("portfolio_notional_twd must be positive")
    if not 0 < max_participation_rate <= 0.2:
        raise ValueError("max_participation_rate must be in (0, 0.2]")
    if impact_coefficient_bps < 0:
        raise ValueError("impact_coefficient_bps must be non-negative")
    if benchmark not in BENCHMARKS:
        raise ValueError(f"unknown benchmark: {benchmark}")
    if weight_mode not in WEIGHT_MODES:
        raise ValueError(f"unknown weight mode: {weight_mode}")
    cache_key = key_factor_validation_tw(
        start_date.isoformat(), end_date.isoformat(), profile, top_n,
        holding_sessions, transaction_cost_bps, sector_neutral,
        portfolio_notional_twd, max_participation_rate, impact_coefficient_bps,
        benchmark, weight_mode,
    )
    cached = await cache_get_json(cache_key)
    if cached is not None:
        FACTOR_ANALYSIS_TOTAL.labels(kind="validation", outcome=cached["quality"]["status"]).inc()
        return cached
    async with AsyncSessionLocal() as db:
        # Load all snapshots once; each fold filters to information available
        # at its anchor. Lifecycle sidecars are loaded independently below.
        fundamental_rows = (await db.scalars(
            select(FundamentalsSnapshot).where(
                FundamentalsSnapshot.market == "TW",
                FundamentalsSnapshot.as_of <= end_date,
            ).order_by(FundamentalsSnapshot.as_of.asc())
        )).all()
        company_rows = (await db.scalars(select(TwCompanyInfo))).all()
        classifications = await _load_classification_history(db, end=end_date)
        bar_rows = (await db.scalars(
            select(OhlcvDaily).where(
                OhlcvDaily.market == "TW",
                OhlcvDaily.ts >= start_date - timedelta(days=550),
                OhlcvDaily.ts <= end_date + timedelta(days=120),
                OhlcvDaily.close.isnot(None),
            ).order_by(OhlcvDaily.symbol.asc(), OhlcvDaily.ts.asc())
        )).all()

    adjusted, stock_info, delistings, lifecycle_catalog_ready = await _load_research_sidecars(
        start=start_date - timedelta(days=550),
        end=end_date + timedelta(days=120),
    )
    quality_history, quality_history_ready = await _load_quality_statement_history(
        start=start_date, end=end_date,
    )
    price_limits, suspensions, limits_available, suspensions_available = (
        await _load_execution_sidecars(
            start=start_date, end=end_date + timedelta(days=120),
        )
    )

    companies = {row.symbol: {"name_zh": row.name_zh, "industry": row.industry}
                 for row in company_rows}
    bars: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sessions: set[date] = set()
    all_sessions: set[date] = set()
    for row in bar_rows:
        bars[row.symbol].append({"date": row.ts.isoformat(), "close": _number(row.close),
                                 "volume": int(row.volume) if row.volume is not None else None})
        all_sessions.add(row.ts)
        if start_date <= row.ts <= end_date:
            sessions.add(row.ts)
    taiex_total_return_bars = bars.pop("_TAIEX_TR", [])
    bars = defaultdict(list, merge_adjusted_prices(dict(bars), adjusted))
    diagnostic_closes = {
        symbol: {
            date.fromisoformat(str(row["date"])[:10]): value
            for row in rows if row.get("date")
            and (value := _number(row.get("close"), positive=True)) is not None
        }
        for symbol, rows in bars.items()
    }
    ordered_sessions = sorted(sessions)
    ordered_all_sessions = sorted(all_sessions)
    anchors = ordered_sessions[::holding_sessions]
    usable_benchmark_anchors = [
        anchor for anchor in anchors
        if ordered_all_sessions.index(anchor) + 1 + holding_sessions < len(ordered_all_sessions)
    ]
    taiex_benchmark_coverage = (
        sum(benchmark_forward_return(
            bars=taiex_total_return_bars, market_sessions=ordered_all_sessions,
            anchor=anchor, holding_sessions=holding_sessions,
        ) is not None for anchor in usable_benchmark_anchors)
        / max(len(usable_benchmark_anchors), 1)
    )
    benchmark_used = (
        "taiex_total_return"
        if benchmark == "taiex_total_return" and taiex_benchmark_coverage >= 0.8
        else "equal_weight"
    )
    snapshots_by_symbol: dict[str, list[Any]] = defaultdict(list)
    for row in fundamental_rows:
        snapshots_by_symbol[row.symbol].append(row)

    periods: list[dict[str, Any]] = []
    factor_correlation_samples: list[dict[str, dict[str, float | None]]] = []
    holding_sensitivity_samples: dict[int, list[dict[str, Any]]] = defaultdict(list)
    top_n_sensitivity_samples: dict[int, list[float]] = defaultdict(list)
    weight_learning_history: list[dict[str, Any]] = []
    previous_holdings: set[str] = set()
    cumulative = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for anchor in anchors:
        fundamentals: dict[str, dict[str, Any]] = {}
        for symbol, rows in snapshots_by_symbol.items():
            available = [row for row in rows if row.as_of <= anchor]
            if not available:
                continue
            row = available[-1]
            fundamentals[symbol] = {
                "as_of": row.as_of.isoformat(), "pe_ratio": _number(row.pe_ratio),
                "pb_ratio": _number(row.pb_ratio), "dividend_yield": _number(row.dividend_yield),
            }
        history = {symbol: [row for row in rows if str(row["date"]) <= anchor.isoformat()]
                   for symbol, rows in bars.items()}
        anchor_companies = point_in_time_companies(
            as_of=anchor, current=companies, stock_info=stock_info,
            delistings=delistings, bars_by_symbol=history,
        )
        anchor_companies, classification_coverage = apply_point_in_time_classifications(
            as_of=anchor, companies=anchor_companies,
            snapshots_by_symbol=classifications,
            universe_symbols=set(fundamentals) & set(history),
        )
        lifecycle_ready = point_in_time_universe_ready(
            catalog_ready=lifecycle_catalog_ready, as_of=anchor,
            delistings=delistings, fundamentals=fundamentals,
            bars_by_symbol=history,
        )
        if weight_mode == "walk_forward":
            anchor_weights, weight_metadata = learn_walk_forward_weights(
                base_weights=PROFILES[profile], learning_history=weight_learning_history,
                as_of=anchor,
            )
        else:
            anchor_weights = dict(PROFILES[profile])
            weight_metadata = {
                "source_period_count": 0,
                "fallback_reason": "fixed_mode",
                "mean_rank_ic": {},
            }
        ranking = build_factor_ranking(
            fundamentals=fundamentals, bars_by_symbol=history, companies=anchor_companies,
            as_of=anchor, profile=profile, limit=10_000, historical=True,
            universe_point_in_time=lifecycle_ready,
            classification_coverage_pct=classification_coverage,
            sector_neutral=sector_neutral,
            quality_by_symbol=point_in_time_quality(
                history=quality_history, as_of=anchor,
            ),
            quality_availability_approximated=quality_history_ready,
            weights_override=anchor_weights,
        )
        diagnostic_anchor_index: int | None = None
        try:
            diagnostic_anchor_index = ordered_all_sessions.index(anchor)
            diagnostic_entry = ordered_all_sessions[diagnostic_anchor_index + 1]
            diagnostic_exit = ordered_all_sessions[
                diagnostic_anchor_index + 1 + holding_sessions
            ]
        except (ValueError, IndexError):
            diagnostic_entry = diagnostic_exit = None
        diagnostic_forward_returns: dict[str, float] = {}
        if diagnostic_entry is not None and diagnostic_exit is not None:
            for candidate in ranking["candidates"]:
                closes = diagnostic_closes.get(candidate["symbol"], {})
                entry_value, exit_value = closes.get(diagnostic_entry), closes.get(diagnostic_exit)
                if entry_value is not None and exit_value is not None:
                    diagnostic_forward_returns[candidate["symbol"]] = exit_value / entry_value - 1
        cross_section = diagnose_factor_cross_section(
            candidates=ranking["candidates"], forward_returns=diagnostic_forward_returns,
        )
        if diagnostic_exit is not None:
            weight_learning_history.append({
                "available_on": diagnostic_exit.isoformat(),
                "rank_ic": {
                    factor: cross_section["rank_ic"].get(factor)
                    for factor in FACTOR_NAMES
                },
            })
        anchor_holding_sensitivity: dict[int, dict[str, Any]] = {}
        for horizon in sorted({5, 21, 63, holding_sessions}):
            if diagnostic_anchor_index is None:
                continue
            try:
                horizon_exit = ordered_all_sessions[
                    diagnostic_anchor_index + 1 + horizon
                ]
            except IndexError:
                continue
            horizon_returns: dict[str, float] = {}
            if diagnostic_entry is not None:
                for candidate in ranking["candidates"]:
                    closes = diagnostic_closes.get(candidate["symbol"], {})
                    entry_value, exit_value = closes.get(diagnostic_entry), closes.get(horizon_exit)
                    if entry_value is not None and exit_value is not None:
                        horizon_returns[candidate["symbol"]] = exit_value / entry_value - 1
            horizon_diagnostic = diagnose_factor_cross_section(
                candidates=ranking["candidates"], forward_returns=horizon_returns,
            )
            anchor_holding_sensitivity[horizon] = {
                "rank_ic": horizon_diagnostic["rank_ic"],
                "top_bottom_spread_pct": horizon_diagnostic["top_bottom_spread_pct"],
            }
        anchor_top_n_sensitivity: dict[int, float] = {}
        for size in sorted({10, 20, 50, top_n}):
            if len(ranking["candidates"]) < size:
                continue
            values = [
                diagnostic_forward_returns[row["symbol"]]
                for row in ranking["candidates"][:size]
                if row["symbol"] in diagnostic_forward_returns
            ]
            if len(values) >= max(3, math.ceil(size * 0.8)):
                anchor_top_n_sensitivity[size] = fmean(values) * 100
        selected = [row["symbol"] for row in ranking["candidates"][:top_n]]
        target_notional = portfolio_notional_twd / max(len(selected), 1)
        trades = [
            simulate_forward_trade(
                symbol=symbol, bars=bars.get(symbol, []),
                market_sessions=ordered_all_sessions, anchor=anchor,
                holding_sessions=holding_sessions,
                target_notional_twd=target_notional,
                max_participation_rate=max_participation_rate,
                impact_coefficient_bps=impact_coefficient_bps,
                price_limits=price_limits, suspensions=suspensions,
            )
            for symbol in selected
        ]
        executed = [trade for trade in trades if trade and trade.get("executed")]
        returns = [
            float(trade["gross_return"]) if trade and trade.get("executed") else 0.0
            for trade in trades
        ]
        benchmark_vol: float | None = None
        if benchmark_used == "taiex_total_return":
            benchmark_return = benchmark_forward_return(
                bars=taiex_total_return_bars, market_sessions=ordered_all_sessions,
                anchor=anchor, holding_sessions=holding_sessions,
            )
            benchmark_vol = benchmark_volatility(
                bars=taiex_total_return_bars, anchor=anchor,
            )
        else:
            eligible_symbols = [row["symbol"] for row in ranking["candidates"]]
            benchmark_target = portfolio_notional_twd / max(len(eligible_symbols), 1)
            benchmark_trades = [
                simulate_forward_trade(
                    symbol=symbol, bars=bars.get(symbol, []),
                    market_sessions=ordered_all_sessions, anchor=anchor,
                    holding_sessions=holding_sessions,
                    target_notional_twd=benchmark_target,
                    max_participation_rate=max_participation_rate,
                    impact_coefficient_bps=impact_coefficient_bps,
                    price_limits=price_limits, suspensions=suspensions,
                )
                for symbol in eligible_symbols
            ]
            benchmark_returns = [
                float(trade["gross_return"])
                if trade and trade.get("executed") else 0.0
                for trade in benchmark_trades
            ]
            benchmark_return = fmean(benchmark_returns) if benchmark_returns else None
        if len(executed) < max(3, len(selected) // 2) or benchmark_return is None:
            continue
        holdings = set(selected)
        turnover = 1.0 if not previous_holdings else 1 - len(holdings & previous_holdings) / max(len(holdings), 1)
        gross = fmean(returns)
        benchmark_period_return = benchmark_return
        impact_cost = fmean(
            float(trade["impact_cost"]) if trade and trade.get("executed") else 0.0
            for trade in trades
        ) if trades else 0.0
        cost = turnover * (transaction_cost_bps / 10_000 + impact_cost)
        net = gross - cost
        cumulative *= 1 + net
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative / peak - 1)
        factor_correlation_samples.append(cross_section["factor_correlations"])
        for horizon, sample in anchor_holding_sensitivity.items():
            holding_sensitivity_samples[horizon].append(sample)
        for size, value in anchor_top_n_sensitivity.items():
            top_n_sensitivity_samples[size].append(value)
        periods.append({
            "anchor": anchor.isoformat(), "holdings": selected,
            "holding_count": len(executed), "turnover": round(turnover, 4),
            "gross_return_pct": round(gross * 100, 4),
            "cost_pct": round(cost * 100, 4), "net_return_pct": round(net * 100, 4),
            "benchmark_return_pct": round(benchmark_period_return * 100, 4),
            "excess_return_pct": round((net - benchmark_period_return) * 100, 4),
            "benchmark_volatility_pct": round(benchmark_vol * 100, 3) if benchmark_vol is not None else None,
            "forward_return_observation_count": cross_section["observation_count"],
            "forward_return_universe_count": len(ranking["candidates"]),
            "forward_return_coverage_pct": round(
                cross_section["observation_count"] / max(len(ranking["candidates"]), 1) * 100, 1,
            ),
            "rank_ic": cross_section["rank_ic"],
            "quintile_returns_pct": cross_section["quintile_returns_pct"],
            "top_bottom_spread_pct": cross_section["top_bottom_spread_pct"],
            "factor_weights": {
                factor: round(float(ranking["weights"][factor]), 6)
                for factor in FACTOR_NAMES
            },
            "weight_source_period_count": weight_metadata["source_period_count"],
            "weight_fallback_reason": weight_metadata["fallback_reason"],
            "quality_status": ranking["quality"]["status"],
            "quality_flags": ranking["quality"]["flags"],
            "classification_coverage_pct": ranking["quality"]["classification_coverage_pct"],
            "sector_coverage_pct": ranking["quality"]["sector_coverage_pct"],
            "sector_neutral_applied": ranking["quality"]["sector_neutral_applied"],
            "average_fill_pct": round(
                fmean(
                    float(trade["fill_ratio"])
                    if trade and trade.get("executed") else 0.0
                    for trade in trades
                ) * 100, 2,
            ),
            "impact_cost_pct": round(impact_cost * 100, 4),
            "capacity_limited_count": sum(
                bool(trade and trade.get("capacity_limited")) for trade in trades
            ),
            "deferred_trade_count": sum(
                bool(trade and trade.get("executed") and (
                    trade.get("entry_delay_sessions", 0) or trade.get("exit_delay_sessions", 0)
                )) for trade in trades
            ),
            "blocked_entry_count": sum(
                bool(trade and trade.get("blocked_side") == "entry") for trade in trades
            ),
            "blocked_exit_count": sum(
                bool(trade and trade.get("blocked_side") == "exit") for trade in trades
            ),
            "capacity_blocked_count": sum(
                bool(trade and trade.get("blocked_side") == "capacity") for trade in trades
            ),
        })
        previous_holdings = holdings

    net_returns = [row["net_return_pct"] / 100 for row in periods]
    excess_returns = [row["excess_return_pct"] / 100 for row in periods]
    inference = excess_return_statistics(excess_returns)
    information_ratio: float | None = None
    if len(excess_returns) >= 2 and stdev(excess_returns) > 0:
        information_ratio = (
            fmean(excess_returns) / stdev(excess_returns)
            * math.sqrt(252 / holding_sessions)
        )
    regime_analysis = build_regime_analysis(periods)
    factor_diagnostics, factor_correlation_matrix, quantile_analysis = (
        aggregate_factor_diagnostics(
            periods=periods, correlation_samples=factor_correlation_samples,
            holding_sessions=holding_sessions,
        )
    )
    sensitivity_analysis = {
        "holding_sessions": {
            str(horizon): {
                "period_count": len(samples),
                "average_rank_ic": round(fmean(values), 4) if (values := [
                    float(sample["rank_ic"]["composite"]) for sample in samples
                    if sample.get("rank_ic", {}).get("composite") is not None
                ]) else None,
                "average_top_bottom_spread_pct": round(fmean(spreads), 4) if (spreads := [
                    float(sample["top_bottom_spread_pct"]) for sample in samples
                    if sample.get("top_bottom_spread_pct") is not None
                ]) else None,
            }
            for horizon, samples in sorted(holding_sensitivity_samples.items())
        },
        "top_n": {
            str(size): {
                "period_count": len(values),
                "average_forward_return_pct": round(fmean(values), 4) if values else None,
            }
            for size, values in sorted(top_n_sensitivity_samples.items())
        },
    }
    factor_decay_analysis: dict[str, dict[str, Any]] = {}
    for signal in DIAGNOSTIC_SIGNALS:
        horizons: dict[str, float | None] = {}
        for horizon, samples in sorted(holding_sensitivity_samples.items()):
            values = [
                float(sample["rank_ic"][signal]) for sample in samples
                if sample.get("rank_ic", {}).get(signal) is not None
            ]
            horizons[str(horizon)] = round(fmean(values), 4) if values else None
        available_horizons = {
            int(horizon): float(value) for horizon, value in horizons.items()
            if value is not None
        }
        peak_horizon = max(
            available_horizons, key=lambda horizon: abs(available_horizons[horizon]),
        ) if available_horizons else None
        signs = {1 if value > 0 else -1 if value < 0 else 0
                 for value in available_horizons.values()}
        factor_decay_analysis[signal] = {
            "average_rank_ic_by_horizon": horizons,
            "peak_absolute_ic_horizon": peak_horizon,
            "direction_consistent": len(signs - {0}) <= 1 if signs else None,
        }
    weight_changes = [
        0.5 * sum(
            abs(float(current["factor_weights"][factor]) - float(previous["factor_weights"][factor]))
            for factor in FACTOR_NAMES
        )
        for previous, current in zip(periods, periods[1:])
    ]
    adaptive_period_count = sum(
        row.get("weight_fallback_reason") is None for row in periods
    ) if weight_mode == "walk_forward" else 0
    fallback_period_count = sum(
        row.get("weight_fallback_reason") is not None for row in periods
    ) if weight_mode == "walk_forward" else 0
    weight_stability = {
        "mode": weight_mode,
        "base_weights": PROFILES[profile],
        "adaptive_period_count": adaptive_period_count,
        "fallback_period_count": fallback_period_count,
        "average_weight_turnover_pct": round(fmean(weight_changes) * 100, 3)
        if weight_changes else 0.0,
        "maximum_weight_turnover_pct": round(max(weight_changes) * 100, 3)
        if weight_changes else 0.0,
        "factor_ranges": {
            factor: {
                "minimum": round(min(values), 6) if (values := [
                    float(row["factor_weights"][factor]) for row in periods
                ]) else None,
                "maximum": round(max(values), 6) if values else None,
                "latest": round(values[-1], 6) if values else None,
            }
            for factor in FACTOR_NAMES
        },
    }
    pit_ready = bool(periods) and all(
        "survivorship_bias" not in row.get("quality_flags", [])
        for row in periods
    )
    aggregate_flags = list(dict.fromkeys(
        [flag for row in periods for flag in row.get("quality_flags", [])]
        + ([] if adjusted else ["unadjusted_price_history"])
        + ([] if pit_ready else ["survivorship_bias"])
        + ([] if limits_available else ["price_limit_history_unavailable"])
        + ([] if suspensions_available else ["suspension_history_unavailable"])
        + (["low_execution_fill"] if periods and fmean(
            row["average_fill_pct"] for row in periods
        ) < 80 else [])
        + (["taiex_total_return_benchmark_unavailable"]
           if benchmark == "taiex_total_return" and benchmark_used == "equal_weight" else [])
        + (["equal_weight_benchmark"] if benchmark_used == "equal_weight" else [])
        + (["insufficient_statistical_sample"] if len(periods) < 12 else [])
        + (["factor_diagnostic_sample_insufficient"]
           if factor_diagnostics["composite"]["period_count"] < 12 else [])
        + (["low_factor_forward_return_coverage"] if periods and fmean(
            row["forward_return_coverage_pct"] for row in periods
        ) < 80 else [])
        + (["walk_forward_weights_unavailable"]
           if weight_mode == "walk_forward" and periods and adaptive_period_count == 0 else [])
        + (["walk_forward_warmup_fallback"]
           if weight_mode == "walk_forward" and fallback_period_count > 0 else [])
        + ["multiple_testing_limited_scope"]
    ))
    material_flags = set(aggregate_flags) - {
        "multiple_testing_limited_scope",
        "financial_statement_availability_approximated",
        "walk_forward_warmup_fallback",
    }
    status = (
        "unavailable" if len(periods) < 3
        else "good" if len(periods) >= 12 and not material_flags
        else "degraded"
    )
    result = {
        "market": "TW", "profile": profile, "methodology_version": METHOD_VERSION,
        "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
        "top_n": top_n, "holding_sessions": holding_sessions,
        "transaction_cost_bps": transaction_cost_bps, "periods": periods,
        "sector_neutral": sector_neutral,
        "portfolio_notional_twd": portfolio_notional_twd,
        "max_participation_rate": max_participation_rate,
        "impact_coefficient_bps": impact_coefficient_bps,
        "benchmark_requested": benchmark,
        "benchmark_used": benchmark_used,
        "weight_mode": weight_mode,
        "summary": {
            "period_count": len(periods),
            "cumulative_return_pct": round((cumulative - 1) * 100, 3) if periods else None,
            "average_period_return_pct": round(fmean(net_returns) * 100, 3) if net_returns else None,
            "average_excess_return_pct": round(fmean(excess_returns) * 100, 3) if excess_returns else None,
            "positive_excess_rate_pct": round(
                sum(value > 0 for value in excess_returns) / len(excess_returns) * 100, 1,
            ) if excess_returns else None,
            "annualized_information_ratio": round(information_ratio, 3) if information_ratio is not None else None,
            **inference,
            "positive_period_rate_pct": round(sum(value > 0 for value in net_returns) / len(net_returns) * 100, 1) if net_returns else None,
            "max_drawdown_pct": round(max_drawdown * 100, 3) if periods else None,
            "average_turnover_pct": round(fmean(row["turnover"] for row in periods) * 100, 1) if periods else None,
            "average_fill_pct": round(fmean(row["average_fill_pct"] for row in periods), 2) if periods else None,
            "average_impact_cost_pct": round(fmean(row["impact_cost_pct"] for row in periods), 4) if periods else None,
            "blocked_trade_count": sum(
                row["blocked_entry_count"] + row["blocked_exit_count"]
                + row["capacity_blocked_count"] for row in periods
            ),
        },
        "regime_analysis": regime_analysis,
        "factor_diagnostics": factor_diagnostics,
        "factor_correlation_matrix": factor_correlation_matrix,
        "quantile_analysis": quantile_analysis,
        "sensitivity_analysis": sensitivity_analysis,
        "factor_decay_analysis": factor_decay_analysis,
        "weight_stability": weight_stability,
        "quality": {
            "status": status,
            "flags": aggregate_flags,
            "adjusted_price_coverage_pct": round(
                sum(bool(row.get("adjusted")) for rows in bars.values() for row in rows)
                / max(sum(len(rows) for rows in bars.values()), 1) * 100, 1,
            ),
            "point_in_time_universe": pit_ready,
            "classification_coverage_pct": round(
                min((row["classification_coverage_pct"] for row in periods), default=0.0), 1,
            ),
            "sector_coverage_pct": round(
                min((row["sector_coverage_pct"] for row in periods), default=0.0), 1,
            ),
            "sector_neutral_applied": bool(periods) and all(
                row["sector_neutral_applied"] for row in periods
            ),
            "price_limit_history_available": limits_available,
            "suspension_history_available": suspensions_available,
            "benchmark_requested": benchmark,
            "benchmark_used": benchmark_used,
            "benchmark_history_available": bool(taiex_total_return_bars),
            "benchmark_coverage_pct": round(taiex_benchmark_coverage * 100, 1),
            "factor_forward_return_coverage_pct": round(fmean(
                row["forward_return_coverage_pct"] for row in periods
            ), 1) if periods else 0.0,
            "sources": [
                "fundamentals_snapshots", "ohlcv_daily", "finmind.tw_stock_price_adj",
                "finmind.tw_stock_info", "finmind.tw_delisting",
                "finmind.tw_price_limit_daily", "finmind.tw_suspended",
                "tw_company_classification_snapshots",
                *(["ohlcv_daily._TAIEX_TR"] if taiex_total_return_bars else []),
            ],
        },
        "methodology": {
            "validation": (
                "rolling out-of-sample forward returns; walk-forward weights use only IC "
                "labels whose holding periods ended by the current anchor"
                if weight_mode == "walk_forward"
                else "rolling out-of-sample forward returns; fixed profile weights, no fitting"
            ),
            "execution": (
                "rank at anchor close; buy from next market session; defer locked/suspended "
                "entry or exit up to 5 sessions"
            ),
            "portfolio": (
                "equal-weight top-N with unfilled capacity held as cash; turnover cost plus "
                "square-root participation-rate market impact"
            ),
            "benchmark": (
                "TAIEX total-return index (_TAIEX_TR / IR0001); explicit equal-weight "
                "eligible-universe fallback when unavailable"
            ),
            "inference": (
                "period-return t-statistic, deterministic 2,000-resample percentile "
                "bootstrap 95% interval, and annualised information ratio"
            ),
            "regimes": (
                "bull/bear by benchmark holding-period return; high/low volatility by "
                "the median 63-session trailing annualised benchmark volatility"
            ),
            "factor_diagnostics": (
                "cost-free next-session forward Rank IC and composite quintile returns; "
                "Holm correction covers the seven signals reported in this response"
            ),
            "sensitivity": (
                "cost-free adjusted forward returns across 5/21/63-session horizons "
                "and top-10/20/50 breadth; diagnostic only"
            ),
            "weight_learning": (
                "minimum 12 mature labels, trailing 24-label window, reliability shrinkage, "
                "and each factor constrained to 50%-150% of its profile base weight"
                if weight_mode == "walk_forward" else "disabled; profile weights remain fixed"
            ),
            "limitations": (
                "Rank IC and quintile spreads are signal diagnostics, not executable returns; "
                "Holm correction cannot account for repeated API runs or unpublished trials"
            ),
        },
    }
    FACTOR_ANALYSIS_TOTAL.labels(kind="validation", outcome=status).inc()
    await cache_set_json(cache_key, result, TTL_FACTOR_VALIDATION)
    return result
