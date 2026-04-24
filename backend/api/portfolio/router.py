from fastapi import APIRouter, Depends, HTTPException, status
from typing import Annotated
from sqlalchemy.ext.asyncio import AsyncSession

from api.portfolio.schemas import (
    PortfolioCreate,
    PortfolioListItem,
    PortfolioSummary,
    TransactionCreate,
    OptimiseRequest,
    OptimiseResponse,
)
from dependencies import get_current_user
from db.session import get_db
import services.portfolio_service as svc

router = APIRouter()
Auth = Annotated[dict, Depends(get_current_user)]
DB   = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=list[PortfolioListItem])
async def list_portfolios(user: Auth, db: DB):
    portfolios = await svc.list_portfolios(user["id"], db)
    return [PortfolioListItem(id=p.id, name=p.name, currency=p.currency) for p in portfolios]


@router.post("", response_model=PortfolioListItem, status_code=status.HTTP_201_CREATED)
async def create_portfolio(body: PortfolioCreate, user: Auth, db: DB):
    p = await svc.create_portfolio(user["id"], body.name, body.currency, db)
    return PortfolioListItem(id=p.id, name=p.name, currency=p.currency)


@router.get("/{portfolio_id}", response_model=PortfolioSummary)
async def get_portfolio(portfolio_id: str, user: Auth, db: DB):
    try:
        return await svc.get_portfolio_detail(portfolio_id, user["id"], db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{portfolio_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_portfolio(portfolio_id: str, user: Auth, db: DB):
    deleted = await svc.delete_portfolio(portfolio_id, user["id"], db)
    if not deleted:
        raise HTTPException(status_code=404, detail="Portfolio not found")


@router.post("/{portfolio_id}/transaction", status_code=status.HTTP_201_CREATED)
async def add_transaction(portfolio_id: str, body: TransactionCreate, user: Auth, db: DB):
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
async def optimise(portfolio_id: str, body: OptimiseRequest, user: Auth, db: DB):
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
