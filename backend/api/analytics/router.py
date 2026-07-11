import asyncio
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Annotated

from api.analytics.schemas import (
    DCFRequest, DCFResponse,
    VaRRequest, VaRResponse,
    BacktestRequest, BacktestResponse,
    StrategyInfo,
)
from auth.permissions import require_analyst
from limiter import limiter
import services.analytics_service as svc

router = APIRouter()
AnalystUser = Annotated[dict, Depends(require_analyst)]


@router.post("/dcf", response_model=DCFResponse)
@limiter.limit("10/minute")
async def dcf(request: Request, body: DCFRequest, _: AnalystUser):
    """
    DCF valuation. Fetches FCF from market data automatically;
    supply 'overrides' to customise any input (wacc, growth_rate_1, etc.).

    Example override body:
    {
      "symbol": "AAPL", "market": "US",
      "overrides": {
        "fcf_history": [90e9, 100e9, 110e9],
        "growth_rate_1": 0.12,
        "growth_rate_2": 0.06,
        "terminal_growth": 0.03,
        "wacc": 0.09,
        "shares": 15.4e9,
        "net_debt": 48e9,
        "current_price": 182.5
      }
    }
    """
    try:
        result = await svc.run_dcf_analysis(body.symbol, body.market, body.overrides)
        if not result:
            raise HTTPException(status_code=400, detail="Insufficient data for DCF")
        return DCFResponse(**result)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Analytics error: {e}")


@router.post("/var", response_model=VaRResponse)
@limiter.limit("5/minute")
async def var(request: Request, body: VaRRequest, _: AnalystUser):
    """
    Portfolio VaR via historical simulation, parametric, or Monte Carlo.
    Use method='all' to get all three in one call.
    """
    if len(body.symbols) != len(body.markets):
        raise HTTPException(status_code=400, detail="symbols and markets must be same length")
    if len(body.weights) != len(body.symbols):
        raise HTTPException(status_code=400, detail="weights must match symbols length")

    try:
        result = await svc.run_var_analysis(
            symbols=body.symbols,
            markets=body.markets,
            weights=body.weights,
            portfolio_value=body.portfolio_value,
            method=body.method,
            confidence=body.confidence,
            horizon_days=body.horizon_days,
        )
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return VaRResponse(**result)
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Computation timed out")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Analytics error: {e}")


@router.get("/backtest/strategies", response_model=list[StrategyInfo])
@limiter.limit("30/minute")
async def backtest_strategies(request: Request, _: AnalystUser):
    """
    Registered built-in strategies with their parameter schemas
    (name / type / default / bounds) — lets the frontend form and any
    sweep tooling introspect instead of hardcoding.
    """
    from analytics.backtest import list_strategies
    return list_strategies()


@router.post("/backtest", response_model=BacktestResponse)
@limiter.limit("5/minute")
async def backtest(request: Request, body: BacktestRequest, _: AnalystUser):
    """
    Event-driven backtest engine.
    Built-in strategies: see GET /backtest/strategies for the registry
    and each strategy's parameter schema.
    Optional risk controls: stop_loss_pct / take_profit_pct /
    trailing_stop_pct / position_size_pct / slippage_bps /
    commission_bps / allow_short — all off by default.
    """
    if len(body.symbols) != len(body.markets):
        raise HTTPException(status_code=400, detail="symbols and markets must be same length")
    if body.start_date >= body.end_date:
        raise HTTPException(status_code=400, detail="start_date must be before end_date")

    try:
        result = await svc.run_backtest_analysis(
            symbols=body.symbols,
            markets=body.markets,
            strategy=body.strategy,
            params=body.params,
            start_date=body.start_date,
            end_date=body.end_date,
            initial_capital=body.initial_capital,
            stop_loss_pct=body.stop_loss_pct,
            take_profit_pct=body.take_profit_pct,
            trailing_stop_pct=body.trailing_stop_pct,
            position_size_pct=body.position_size_pct,
            slippage_bps=body.slippage_bps,
            commission_bps=body.commission_bps,
            allow_short=body.allow_short,
        )
        return BacktestResponse(**result)
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Backtest timed out (30s limit)")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Backtest error: {e}")
