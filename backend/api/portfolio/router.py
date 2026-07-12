import logging
from datetime import date as _date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from typing import Annotated
from sqlalchemy.ext.asyncio import AsyncSession

from api.portfolio.schemas import (
    PortfolioCreate,
    PortfolioListItem,
    PortfolioUpdate,
    PerformancePoint,
    PortfolioSummary,
    TransactionCreate,
    TransactionResponse,
    TransactionUpdate,
    OptimiseRequest,
    OptimiseResponse,
    PortfolioRiskResponse,
    RebalancePlanRequest,
    RebalancePlanResponse,
)
from dependencies import get_current_user
from db.session import get_db
from limiter import limiter
import services.portfolio_service as svc

log = logging.getLogger(__name__)

router = APIRouter()
CurrentUser = Annotated[dict, Depends(get_current_user)]
DB   = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=list[PortfolioListItem])
async def list_portfolios(user: CurrentUser, db: DB):
    portfolios = await svc.list_portfolios(user["id"], db)
    return [PortfolioListItem(id=p.id, name=p.name, currency=p.currency) for p in portfolios]


@router.post("", response_model=PortfolioListItem, status_code=status.HTTP_201_CREATED)
@limiter.limit("20/minute")
async def create_portfolio(request: Request, body: PortfolioCreate, user: CurrentUser, db: DB):
    p = await svc.create_portfolio(user["id"], body.name, body.currency, db)
    return PortfolioListItem(id=p.id, name=p.name, currency=p.currency)


@router.get("/{portfolio_id}", response_model=PortfolioSummary)
async def get_portfolio(portfolio_id: str, user: CurrentUser, db: DB):
    try:
        return await svc.get_portfolio_detail(portfolio_id, user["id"], db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        # Catch-all so any unexpected exception path leaves a stack
        # trace in the logs (instead of just FastAPI's anonymous 500
        # response). The per-holding `_enrich` already swallows
        # individual quote/FX failures into a degraded row, so anything
        # that lands here is a genuine bug — DB schema drift, malformed
        # input, dependency outage — that we want to see immediately.
        log.exception(
            "portfolio.detail_failed",
            extra={"portfolio_id": portfolio_id, "user_id": user["id"]},
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to load portfolio detail; check server logs.",
        )


@router.delete("/{portfolio_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("20/minute")
async def delete_portfolio(request: Request, portfolio_id: str, user: CurrentUser, db: DB):
    deleted = await svc.delete_portfolio(portfolio_id, user["id"], db)
    if not deleted:
        raise HTTPException(status_code=404, detail="Portfolio not found")


@router.patch("/{portfolio_id}", response_model=PortfolioListItem)
@limiter.limit("30/minute")
async def update_portfolio(
    request: Request, portfolio_id: str, body: PortfolioUpdate, user: CurrentUser, db: DB,
):
    """Rename a portfolio and/or change its base currency."""
    p = await svc.update_portfolio(
        portfolio_id, user["id"], db,
        name=body.name, currency=body.currency,
    )
    if not p:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return PortfolioListItem(id=p.id, name=p.name, currency=p.currency)


@router.post("/{portfolio_id}/transaction", status_code=status.HTTP_201_CREATED)
@limiter.limit("60/minute")
async def add_transaction(request: Request, portfolio_id: str, body: TransactionCreate, user: CurrentUser, db: DB):
    try:
        tx = await svc.add_transaction(
            portfolio_id=portfolio_id,
            user_id=user["id"],
            symbol=body.symbol,
            market=body.market,
            tx_type=body.tx_type,
            quantity=body.quantity,
            price=body.price,
            fx_rate=body.fx_rate,
            tx_date=body.tx_date,
            notes=body.notes,
            db=db,
        )
        return {"id": str(tx.id), "status": "created"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{portfolio_id}/optimise", response_model=OptimiseResponse)
@limiter.limit("5/minute")
async def optimise(request: Request, portfolio_id: str, body: OptimiseRequest, user: CurrentUser, db: DB):
    """
    Runs mean-variance portfolio optimisation on current holdings.
    Returns suggested weights — does NOT auto-execute any trades.
    """
    try:
        result = await svc.optimise_portfolio(
            portfolio_id=portfolio_id,
            user_id=user["id"],
            target_risk=body.target_risk,
            max_weight=body.max_weight,
            db=db,
        )
        return OptimiseResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Optimisation error: {e}")


@router.post("/{portfolio_id}/rebalance-plan", response_model=RebalancePlanResponse)
@limiter.limit("5/minute")
async def rebalance_plan(request: Request, portfolio_id: str, body: RebalancePlanRequest, user: CurrentUser, db: DB):
    """Rebalance preview (feature C5) — compares current weights against a
    target (Markowitz optimise / equal weight / custom) and returns a
    minimal trade list with lot rounding (TW 1000-share board lots),
    fee estimates and dust-trade suppression. Preview ONLY — nothing is
    executed. Holdings the target can't cover are frozen, not sold."""
    from services.rebalance_service import build_rebalance_plan
    try:
        result = await build_rebalance_plan(
            portfolio_id,
            user["id"],
            db,
            target=body.target,
            target_risk=body.target_risk,
            max_weight=body.max_weight,
            custom_weights=body.custom_weights,
            fee_bps=body.fee_bps,
            min_trade_pct=body.min_trade_pct,
            allow_odd_lot=body.allow_odd_lot,
        )
        return RebalancePlanResponse(**result)
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Rebalance error: {e}")


@router.get("/{portfolio_id}/risk", response_model=PortfolioRiskResponse)
@limiter.limit("10/minute")
async def portfolio_risk(request: Request, portfolio_id: str, user: CurrentUser, db: DB):
    """Risk dashboard (feature C1) — three-method VaR (95/99), vol /
    Sharpe / Sortino / max drawdown / beta vs benchmark, per-holding
    weight + risk contribution, pairwise correlation matrix, and
    concentration warnings, computed on demand from the user's actual
    holdings. Holdings with insufficient history land in `excluded`;
    an empty portfolio returns `empty: true` with HTTP 200."""
    from services.portfolio_risk_service import get_portfolio_risk
    try:
        result = await get_portfolio_risk(portfolio_id, user["id"], db)
        return PortfolioRiskResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        log.exception(
            "portfolio.risk_failed",
            extra={"portfolio_id": portfolio_id, "user_id": user["id"]},
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to compute portfolio risk; check server logs.",
        )


@router.get("/{portfolio_id}/performance", response_model=list[PerformancePoint])
async def performance(portfolio_id: str, user: CurrentUser, db: DB, days: int = 90):
    """Daily portfolio value snapshots for the last N days."""
    try:
        return await svc.get_performance(portfolio_id, user["id"], db, days=days)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{portfolio_id}/transactions", response_model=list[TransactionResponse])
async def list_transactions(portfolio_id: str, user: CurrentUser, db: DB, limit: int = 200):
    """All transactions for a portfolio, newest first."""
    try:
        return await svc.get_transactions(portfolio_id, user["id"], db, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/{portfolio_id}/transactions/{tx_id}", response_model=TransactionResponse)
@limiter.limit("60/minute")
async def update_transaction(
    request: Request, portfolio_id: str, tx_id: str, body: TransactionUpdate, user: CurrentUser, db: DB,
):
    """Edit fields on an existing transaction. Re-derives the affected
    holding(s); if symbol/market changed, both old and new holdings rebuild."""
    tx = await svc.update_transaction(
        portfolio_id, tx_id, user["id"], db,
        symbol=body.symbol,
        market=body.market,
        tx_type=body.tx_type,
        quantity=body.quantity,
        price=body.price,
        fx_rate=body.fx_rate,
        tx_date=body.tx_date,
        notes=body.notes,
    )
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return tx


@router.delete("/{portfolio_id}/transactions/{tx_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("60/minute")
async def delete_transaction(request: Request, portfolio_id: str, tx_id: str, user: CurrentUser, db: DB):
    """Remove one transaction; rebuilds the affected holding from remaining txs."""
    deleted = await svc.delete_transaction(portfolio_id, tx_id, user["id"], db)
    if not deleted:
        raise HTTPException(status_code=404, detail="Transaction not found")


@router.get("/{portfolio_id}/fx-rate")
async def fx_rate_for_transaction(
    portfolio_id: str,
    user: CurrentUser,
    db: DB,
    market: str = Query(..., pattern=r"^(US|TW|CRYPTO|us|tw|crypto)$"),
    tx_date: _date = Query(..., description="Trade date (YYYY-MM-DD)"),
):
    """Suggested per-unit fx_rate to stamp on a transaction in this
    portfolio on the given date, so the frontend can pre-fill the FX
    field. Returns 1.0 when no conversion is needed."""
    p = await svc.get_portfolio(portfolio_id, user["id"], db)
    if not p:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    rate = await svc.get_default_fx_rate(market, p.currency, tx_date)
    return {
        "portfolio_id":       portfolio_id,
        "portfolio_currency": p.currency,
        "market":             market.upper(),
        "tx_date":            tx_date.isoformat(),
        "fx_rate":            round(rate, 6),
    }
