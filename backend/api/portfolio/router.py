import logging
from datetime import date as _date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

import services.portfolio_service as svc
from api.portfolio.schemas import (
    CashBalanceResponse,
    CashEntryCreate,
    CashEntryResponse,
    CashEntryReverse,
    OptimiseRequest,
    OptimiseResponse,
    PerformancePoint,
    PortfolioAttributionResponse,
    PortfolioCreate,
    PortfolioListItem,
    PortfolioRiskResponse,
    PortfolioSnapshotResponse,
    PortfolioSummary,
    PortfolioUpdate,
    RebalancePlanRequest,
    RebalancePlanResponse,
    StressTestRequest,
    StressTestResponse,
    TransactionCreate,
    TransactionImportRequest,
    TransactionImportResponse,
    TransactionResponse,
    TransactionUpdate,
)
from db.session import get_db
from dependencies import get_current_user
from limiter import limiter

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
        code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=code, detail=str(e))
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
        code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=code, detail=str(e))


@router.post(
    "/{portfolio_id}/transactions/import",
    response_model=TransactionImportResponse,
)
@limiter.limit("10/minute")
async def import_transactions(
    request: Request,
    portfolio_id: str,
    body: TransactionImportRequest,
    user: CurrentUser,
    db: DB,
):
    """Validate up to 500 CSV rows, then atomically import a clean batch."""
    try:
        return await svc.import_transactions(
            portfolio_id=portfolio_id,
            user_id=user["id"],
            rows=body.rows,
            dry_run=body.dry_run,
            db=db,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=code, detail=str(exc))


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


@router.post("/{portfolio_id}/stress-test", response_model=StressTestResponse)
@limiter.limit("10/minute")
async def portfolio_stress_test(
    request: Request, portfolio_id: str, body: StressTestRequest, user: CurrentUser, db: DB,
):
    """Apply transparent TAIEX, semiconductor, FX, rate and gap shocks.

    This endpoint is a deterministic preview and never executes trades.
    """
    from services.portfolio_stress_service import stress_test_portfolio
    try:
        return StressTestResponse(**await stress_test_portfolio(
            portfolio_id, user["id"], db,
            scenarios=body.scenarios, gap_symbol=body.gap_symbol, gap_pct=body.gap_pct,
        ))
    except ValueError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{portfolio_id}/performance", response_model=list[PerformancePoint])
async def performance(portfolio_id: str, user: CurrentUser, db: DB, days: int = 90):
    """Daily portfolio value snapshots for the last N days."""
    try:
        return await svc.get_performance(portfolio_id, user["id"], db, days=days)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{portfolio_id}/attribution", response_model=PortfolioAttributionResponse)
@limiter.limit("10/minute")
async def portfolio_attribution(
    request: Request,
    portfolio_id: str,
    user: CurrentUser,
    db: DB,
    days: int = Query(default=90),
):
    """Transaction-flow-adjusted Modified Dietz return attribution."""
    from services.portfolio_attribution_service import get_portfolio_attribution

    try:
        return PortfolioAttributionResponse(**await get_portfolio_attribution(
            portfolio_id, user["id"], db, days=days,
        ))
    except ValueError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{portfolio_id}/transactions", response_model=list[TransactionResponse])
async def list_transactions(portfolio_id: str, user: CurrentUser, db: DB, limit: int = 200):
    """All transactions for a portfolio, newest first."""
    try:
        return await svc.get_transactions(portfolio_id, user["id"], db, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{portfolio_id}/snapshots", response_model=list[PortfolioSnapshotResponse])
async def portfolio_snapshots(
    portfolio_id: str, user: CurrentUser, db: DB,
    days: int = Query(default=90, ge=1, le=3650),
):
    """Rich daily holdings/cash snapshots for audit and historical replay."""
    try:
        return await svc.get_portfolio_snapshots(
            portfolio_id, user["id"], db, days=days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{portfolio_id}/cash", response_model=CashBalanceResponse)
async def cash_balance(
    portfolio_id: str, user: CurrentUser, db: DB, as_of: _date | None = None,
):
    """Multi-currency ledger balance, converted to the portfolio base currency."""
    from services import portfolio_cash_service as cash_svc

    try:
        portfolio = await svc.get_portfolio(portfolio_id, user["id"], db)
        if not portfolio:
            raise ValueError("Portfolio not found")
        balances = await cash_svc.get_cash_balances(
            portfolio_id=portfolio_id, user_id=user["id"], db=db, as_of=as_of,
        )
        total = await cash_svc.cash_value_in_currency(
            balances=balances, target_currency=portfolio.currency,
        )
        return {
            "portfolio_id": portfolio_id, "base_currency": portfolio.currency,
            "balances": balances, "total_cash_base": round(total, 2),
            "negative_currencies": sorted(
                currency for currency, amount in balances.items() if amount < -1e-6
            ),
            "as_of": as_of,
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{portfolio_id}/cash-entries", response_model=list[CashEntryResponse])
async def cash_entries(
    portfolio_id: str, user: CurrentUser, db: DB,
    limit: int = Query(default=200, ge=1, le=1000),
):
    from services import portfolio_cash_service as cash_svc

    try:
        return await cash_svc.list_entries(
            portfolio_id=portfolio_id, user_id=user["id"], db=db, limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post(
    "/{portfolio_id}/cash-entries", response_model=CashEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("30/minute")
async def create_cash_entry(
    request: Request, portfolio_id: str, body: CashEntryCreate,
    user: CurrentUser, db: DB,
):
    from services import portfolio_cash_service as cash_svc

    try:
        return await cash_svc.create_manual_entry(
            portfolio_id=portfolio_id, user_id=user["id"], currency=body.currency,
            amount=body.amount, entry_type=body.entry_type,
            occurred_on=body.occurred_on, notes=body.notes,
            idempotency_key=body.idempotency_key, db=db,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=code, detail=str(exc))


@router.post(
    "/{portfolio_id}/cash-entries/{entry_id}/reverse",
    response_model=CashEntryResponse,
)
@limiter.limit("30/minute")
async def reverse_cash_entry(
    request: Request, portfolio_id: str, entry_id: str, body: CashEntryReverse,
    user: CurrentUser, db: DB,
):
    from services import portfolio_cash_service as cash_svc

    try:
        return await cash_svc.reverse_entry(
            portfolio_id=portfolio_id, entry_id=entry_id, user_id=user["id"],
            db=db, notes=body.notes,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=code, detail=str(exc))


@router.patch("/{portfolio_id}/transactions/{tx_id}", response_model=TransactionResponse)
@limiter.limit("60/minute")
async def update_transaction(
    request: Request, portfolio_id: str, tx_id: str, body: TransactionUpdate, user: CurrentUser, db: DB,
):
    """Edit fields on an existing transaction. Re-derives the affected
    holding(s); if symbol/market changed, both old and new holdings rebuild."""
    try:
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
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return tx


@router.delete("/{portfolio_id}/transactions/{tx_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("60/minute")
async def delete_transaction(request: Request, portfolio_id: str, tx_id: str, user: CurrentUser, db: DB):
    """Remove one transaction; rebuilds the affected holding from remaining txs."""
    try:
        deleted = await svc.delete_transaction(portfolio_id, tx_id, user["id"], db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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
