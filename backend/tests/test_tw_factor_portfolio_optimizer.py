from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from analytics.tw_factor_portfolio_optimizer import optimize_factor_portfolio
from models.ohlcv_daily import OhlcvDaily
from services import tw_factor_portfolio_service as portfolio_svc


def _optimizer_inputs(count: int = 20) -> dict:
    covariance = np.full((count, count), .005)
    np.fill_diagonal(covariance, .02)
    return {
        "symbols": [f"S{index:02d}" for index in range(count)],
        "scores": [100 - index for index in range(count)],
        "industries": [f"sector-{index % 4}" for index in range(count)],
        "annual_covariance": covariance,
        "benchmark_covariance": np.full(count, .006),
        "benchmark_variance": .01,
        "liquidity_caps": [.10] * count,
    }


def test_optimizer_respects_position_sector_risk_and_cash_constraints():
    result = optimize_factor_portfolio(
        **_optimizer_inputs(), max_position_weight=.10, max_sector_weight=.30,
        target_volatility=.20, max_tracking_error=.12,
        minimum_invested_weight=.80,
    )
    assert result["converged"] is True
    assert .80 <= sum(result["weights"].values()) <= 1
    assert max(result["weights"].values()) <= .10 + 1e-6
    assert max(result["sector_weights"].values()) <= .30 + 1e-6
    assert result["summary"]["annual_volatility"] <= .20 + 1e-6
    assert result["summary"]["tracking_error"] <= .12 + 1e-6
    assert max(result["violations"].values()) <= 1e-5


def test_optimizer_returns_explicit_infeasible_result_without_equal_weight_fallback():
    result = optimize_factor_portfolio(
        **_optimizer_inputs(5), max_position_weight=.05,
        minimum_invested_weight=.80, target_volatility=1, max_tracking_error=1,
    )
    assert result["converged"] is False
    assert result["weights"] == {}
    assert result["violations"]["minimum_invested_weight"] > 0


def test_turnover_counts_positions_outside_candidate_universe():
    inputs = _optimizer_inputs(20)
    result = optimize_factor_portfolio(
        **inputs, current_weights={"OLD": .50}, turnover_budget=.10,
        minimum_invested_weight=.80, target_volatility=1, max_tracking_error=1,
    )
    assert result["converged"] is False
    assert result["weights"] == {}
    assert result["violations"]["turnover_budget"] > 0


def test_optimizer_rejects_negative_current_weights():
    with pytest.raises(ValueError, match="non-negative"):
        optimize_factor_portfolio(
            **_optimizer_inputs(), current_weights={"S00": -.1},
        )
    with pytest.raises(ValueError, match="sum above 1"):
        optimize_factor_portfolio(
            **_optimizer_inputs(), current_weights={"S00": .7, "S01": .4},
        )


@pytest.mark.asyncio
async def test_portfolio_service_builds_positions_from_archived_prices(
    db_session: AsyncSession,
):
    first = date(2024, 1, 1)
    symbols = [f"8{index:03d}" for index in range(10)]
    for symbol_index, symbol in enumerate([*symbols, "_TAIEX_TR"]):
        price = 100.0 + symbol_index
        for day_index in range(300):
            session = first + timedelta(days=day_index)
            price *= 1 + .0002 + (symbol_index % 3) * .00005
            db_session.add(OhlcvDaily(
                market="TW", symbol=symbol, ts=session,
                open=price, high=price, low=price, close=price,
                volume=0 if symbol == "_TAIEX_TR" else 5_000_000,
                source="test",
            ))
    await db_session.flush()
    ranking = {
        "profile": "balanced", "methodology_version": "tw-explainable-multifactor-v8",
        "weight_source": "profile", "model_id": None,
        "candidates": [
            {
                "symbol": symbol, "name_zh": symbol,
                "industry": f"產業{index % 4}", "score": 100 - index,
            }
            for index, symbol in enumerate(symbols)
        ],
    }
    with patch.object(
        portfolio_svc, "_load_research_sidecars", new_callable=AsyncMock,
        return_value=({}, {}, {}, False),
    ):
        result = await portfolio_svc.construct_factor_portfolio(
            ranking=ranking, as_of=first + timedelta(days=299), candidate_count=10,
            max_position_weight=.20, max_sector_weight=.60,
            target_volatility=1, max_tracking_error=1,
            minimum_invested_weight=.80,
        )
    assert result["converged"] is True
    assert len(result["positions"]) >= 5
    assert result["summary"]["invested_weight"] >= .80
    assert all(item["notional_twd"] > 0 for item in result["positions"])
    assert all(constraint["passed"] for constraint in result["constraints"])
