import asyncio
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from typing import Annotated

from sqlalchemy.ext.asyncio import AsyncSession

from api.analytics.schemas import (
    DCFRequest, DCFResponse,
    VaRRequest, VaRResponse,
    BacktestRequest, BacktestResponse,
    BacktestRunDetail, BacktestRunListResponse, BacktestRunSummary,
    BacktestCompareResponse,
    StrategyInfo,
)
from auth.permissions import require_analyst
from db.session import get_db
from limiter import limiter
import services.analytics_service as svc
import services.backtest_run_service as run_svc

router = APIRouter()
AnalystUser = Annotated[dict, Depends(require_analyst)]
Db = Annotated[AsyncSession, Depends(get_db)]


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
async def backtest(request: Request, body: BacktestRequest, user: AnalystUser, db: Db):
    """
    Event-driven backtest engine.
    Built-in strategies: see GET /backtest/strategies for the registry
    and each strategy's parameter schema.
    Optional risk controls: stop_loss_pct / take_profit_pct /
    trailing_stop_pct / position_size_pct / slippage_bps /
    commission_bps / allow_short — all off by default.
    C3: `save=true` (+ optional `name`) persists a completed run to
    `backtest_runs`; the response then carries `run_id`.
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
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Backtest timed out (30s limit)")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Backtest error: {e}")

    response = BacktestResponse(**result)
    if body.save and result.get("status") == "completed":
        run = await run_svc.save_run(
            db,
            user_id=uuid.UUID(user["id"]),
            name=body.name,
            strategy=body.strategy,
            params=body.params,
            config={
                "symbols": body.symbols,
                "markets": body.markets,
                "start_date": body.start_date,
                "end_date": body.end_date,
                "initial_capital": body.initial_capital,
                "stop_loss_pct": body.stop_loss_pct,
                "take_profit_pct": body.take_profit_pct,
                "trailing_stop_pct": body.trailing_stop_pct,
                "position_size_pct": body.position_size_pct,
                "slippage_bps": body.slippage_bps,
                "commission_bps": body.commission_bps,
                "allow_short": body.allow_short,
            },
            result=result,
        )
        response.run_id = run.id
    return response


# ── C3: persisted backtest runs ───────────────────────────────────

def _run_summary(run) -> BacktestRunSummary:
    return BacktestRunSummary(
        id=run.id, name=run.name, strategy=run.strategy,
        created_at=run.created_at,
        config=run.config or {}, metrics=run.metrics or {},
    )


@router.get("/backtest-runs", response_model=BacktestRunListResponse)
@limiter.limit("30/minute")
async def list_backtest_runs(
    request: Request,
    user: AnalystUser,
    db: Db,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """The caller's saved runs, newest first (paginated)."""
    rows, total = await run_svc.list_runs(
        db, uuid.UUID(user["id"]), limit=limit, offset=offset,
    )
    return BacktestRunListResponse(
        items=[_run_summary(r) for r in rows],
        total=total, limit=limit, offset=offset,
    )


@router.get("/backtest-runs/compare", response_model=BacktestCompareResponse)
@limiter.limit("30/minute")
async def compare_backtest_runs(
    request: Request,
    user: AnalystUser,
    db: Db,
    ids: str = Query(..., description="comma-separated run ids (2-4)"),
):
    """Side-by-side comparison of up to 4 saved runs.

    Equity curves are each normalised to 100 at their own first bar
    and aligned on the union of dates (None = run has no bar that
    day); metrics are returned verbatim per run.
    """
    try:
        run_ids = [uuid.UUID(part.strip()) for part in ids.split(",") if part.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids must be comma-separated UUIDs")
    if not run_ids:
        raise HTTPException(status_code=400, detail="At least one run id is required")
    if len(run_ids) > run_svc.MAX_COMPARE_RUNS:
        raise HTTPException(
            status_code=400,
            detail=f"At most {run_svc.MAX_COMPARE_RUNS} runs can be compared",
        )
    if len(set(run_ids)) != len(run_ids):
        raise HTTPException(status_code=400, detail="Duplicate run ids")

    result = await run_svc.compare_runs(db, uuid.UUID(user["id"]), run_ids)
    if result is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return BacktestCompareResponse(**result)


@router.get("/backtest-runs/{run_id}", response_model=BacktestRunDetail)
@limiter.limit("30/minute")
async def get_backtest_run(
    request: Request, run_id: uuid.UUID, user: AnalystUser, db: Db,
):
    """One saved run with its full equity curve and (capped) trades.
    404 for other users' runs — indistinguishable from non-existent."""
    run = await run_svc.get_run(db, uuid.UUID(user["id"]), run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return BacktestRunDetail(
        id=run.id, name=run.name, strategy=run.strategy,
        created_at=run.created_at,
        config=run.config or {}, metrics=run.metrics or {},
        params=run.params or {},
        equity_curve=run.equity_curve or [],
        trades=run.trades,
    )


@router.delete("/backtest-runs/{run_id}")
@limiter.limit("30/minute")
async def delete_backtest_run(
    request: Request, run_id: uuid.UUID, user: AnalystUser, db: Db,
):
    """Delete one of the caller's saved runs."""
    deleted = await run_svc.delete_run(db, uuid.UUID(user["id"]), run_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"status": "deleted", "id": str(run_id)}
