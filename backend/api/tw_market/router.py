from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

import services.intraday_service as intraday_svc
import services.tw_factor_portfolio_service as factor_portfolio_svc
import services.tw_factor_registry_service as factor_registry_svc
import services.tw_factor_service as factor_svc
import services.tw_market_service as svc
from api.market_data_quality import build_quality_meta
from api.tw_market.schemas import (
    FactorModelVersionResponse,
    FactorPortfolioRequest,
    FactorPortfolioResponse,
    FactorRankingResponse,
    FactorRebalancePreviewRequest,
    FactorRebalancePreviewResponse,
    FactorResearchCreated,
    FactorResearchRequest,
    FactorResearchRunDetail,
    FactorResearchRunList,
    FactorValidationResponse,
    TWFundamentalsResponse,
    TWIndexResponse,
    TWOHLCVBar,
    TWQuoteResponse,
    TWScreenerItem,
    TWSecurityMasterOverrideRequest,
    TWSecurityMasterResponse,
)
from auth.permissions import require_admin
from db.session import get_db
from dependencies import get_current_user

router = APIRouter()
CurrentUser = Annotated[dict, Depends(get_current_user)]
Db = Annotated[AsyncSession, Depends(get_db)]
AdminUser = Annotated[dict, Depends(require_admin)]


def _quality_rows(rows: list[dict], fallback_chain: list[str]) -> list[dict]:
    return [
        {**row, "meta": build_quality_meta(
            row,
            kind="dataset",
            as_of=row.get("date") or row.get("published_at") or row.get("as_of"),
            fallback_chain=fallback_chain,
        ).model_dump()}
        for row in rows
    ]


@router.get(
    "/security-master/{symbol}", response_model=TWSecurityMasterResponse,
)
async def security_master(
    symbol: str,
    _: CurrentUser,
    db: Db,
    as_of: date | None = Query(None),
):
    """Return the traceable classification and trading rule effective on a date."""
    from services.tw_security_master_service import resolve_security_profiles

    day = as_of or date.today()
    normalized = symbol.strip().upper()
    if not normalized:
        raise HTTPException(status_code=422, detail="symbol is required")
    return (await resolve_security_profiles(db, [normalized], as_of=day))[normalized]


@router.post("/security-master/sync")
async def sync_security_master_endpoint(
    _: AdminUser,
    db: Db,
    as_of: date | None = Query(None),
):
    """Admin-only idempotent materialization from the latest TWSE/TPEx master."""
    from services.tw_security_master_service import sync_security_master

    return await sync_security_master(db, as_of=as_of or date.today())


@router.put(
    "/security-master/{symbol}/override",
    response_model=TWSecurityMasterResponse,
)
async def override_security_master(
    symbol: str,
    body: TWSecurityMasterOverrideRequest,
    admin: AdminUser,
    db: Db,
):
    """Admin-only effective-dated correction with actor and reason audit fields."""
    from services.tw_security_master_service import upsert_manual_override

    values = body.model_dump(exclude={"reason", "effective_from", "effective_to"})
    try:
        return await upsert_manual_override(
            db,
            symbol=symbol,
            effective_from=body.effective_from,
            effective_to=body.effective_to,
            values=values,
            reason=body.reason,
            admin_id=str(admin["id"]),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/fundamentals/{symbol}", response_model=TWFundamentalsResponse)
async def fundamentals(symbol: str, _: CurrentUser):
    """本益比、股價淨值比、殖利率 from TWSE BWIBBU_d."""
    try:
        data = await svc.get_fundamentals(symbol)
        data["meta"] = build_quality_meta(
            data, kind="fundamentals",
            fallback_chain=["postgres_recent", "twse", "postgres_stale"],
        ).model_dump()
        return data
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/quote/{symbol}", response_model=TWQuoteResponse)
async def quote(
    symbol: str,
    _: CurrentUser,
    verify: bool = Query(False, description="Cross-check settled data against an independent provider"),
):
    try:
        data = await svc.get_quote(symbol)
        if verify:
            data["quality_check"] = await svc.verify_quote_consistency(symbol, data)
        data["meta"] = build_quality_meta(
            data, kind="quote",
            fallback_chain=["twse_mis", "twse", "finmind", "postgres_snapshot"],
        ).model_dump()
        return data
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/history/{symbol}", response_model=list[TWOHLCVBar])
async def history(
    symbol: str,
    _: CurrentUser,
    months: int = Query(12, ge=1, le=60, description="Number of months of history"),
):
    try:
        bars = await svc.get_history(symbol, months=months)
        anchor = bars[-1].get("time") if bars else None
        return [
            {**bar, "meta": build_quality_meta(
                bar, kind="history", as_of=anchor,
                fallback_chain=["postgres", "twse", "finmind", "postgres_stale"],
            ).model_dump()}
            for bar in bars
        ]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/intraday/{symbol}", response_model=intraday_svc.IntradayResponse)
async def intraday(
    symbol: str,
    _: CurrentUser,
    interval: str = Query("5m", pattern="^(1m|5m|15m)$", description="1m 5m 15m"),
):
    """分時 K 線 — 由 quote_snapshots 每分鐘快照聚合;僅涵蓋快照保留
    窗口(`coverage_days`,現為 30 天)。無快照時回傳空 `bars`(200)。"""
    return await intraday_svc.get_intraday("TW", symbol, interval)


@router.get("/institutional/{symbol}", response_model=list[dict])
async def institutional(
    symbol: str,
    _: CurrentUser,
    days: int = Query(30, ge=1, le=365),
):
    """法人買賣超 — foreign investors, investment trusts, dealers."""
    try:
        return _quality_rows(
            await svc.get_institutional(symbol, days=days),
            ["postgres", "finmind", "twse"],
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/margin/{symbol}", response_model=list[dict])
async def margin(
    symbol: str,
    _: CurrentUser,
    days: int = Query(30, ge=1, le=365),
):
    """融資融券 — margin purchase and short sale balances."""
    try:
        return _quality_rows(
            await svc.get_margin(symbol, days=days),
            ["postgres", "finmind", "twse"],
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/revenue/{symbol}", response_model=list[dict])
async def revenue(
    symbol: str,
    _: CurrentUser,
    months: int = Query(12, ge=1, le=36),
):
    """月營收 — monthly revenue with MoM and YoY growth rates."""
    try:
        return _quality_rows(
            await svc.get_revenue(symbol, months=months),
            ["postgres", "finmind", "mops"],
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/financials/{symbol}", response_model=list[dict])
async def financials(symbol: str, _: CurrentUser):
    """財報 (XBRL) via FinMind."""
    try:
        return await svc.get_financials(symbol)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/health/{symbol}")
async def health(symbol: str, _: CurrentUser, periods: int = Query(8, ge=1, le=20)):
    """財務體質 — derived margins, leverage, liquidity ratios with red/yellow/green lights."""
    try:
        return await svc.get_health(symbol, periods=periods)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/valuation-band/{symbol}")
async def valuation_band(
    symbol: str,
    _: CurrentUser,
    metric: str = Query("pe", pattern="^(pe|pb)$"),
    years: int = Query(5, ge=1, le=10),
):
    """估值帶 (PE / PB band) — daily series + mean / std / percentile stats."""
    try:
        return await svc.get_valuation_band(symbol, metric=metric, years=years)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/dividends/{symbol}", response_model=list[dict])
async def dividends(symbol: str, _: CurrentUser):
    """配息歷史 — works for both ordinary stocks and ETFs."""
    try:
        return await svc.get_dividends(symbol)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/etf/{symbol}/holdings")
async def etf_holdings(symbol: str, _: CurrentUser):
    """ETF 持股明細 — latest snapshot of constituents and weights."""
    try:
        return await svc.get_etf_holdings(symbol)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/screener", response_model=list[TWScreenerItem])
async def screener(
    _: CurrentUser,
    exchange: str | None = Query(None, description="TWSE | TPEx"),
    min_volume: int | None = Query(None, description="Minimum trading volume (shares)"),
    min_pe: float | None = Query(None, description="Minimum P/E ratio (本益比)"),
    max_pe: float | None = Query(None, description="Maximum P/E ratio (本益比)"),
    min_pb: float | None = Query(None, description="Minimum P/B ratio (股價淨值比)"),
    max_pb: float | None = Query(None, description="Maximum P/B ratio (股價淨值比)"),
    min_dividend_yield: float | None = Query(
        None, description="Minimum dividend yield % (殖利率)"
    ),
    include_etf: bool = Query(True, description="Include ETF symbols (00xxx)"),
    etf_only: bool = Query(False, description="Restrict results to ETFs only"),
    limit: int = Query(100, le=500),
):
    try:
        return await svc.get_screener(
            exchange=exchange,
            min_volume=min_volume,
            min_pe=min_pe,
            max_pe=max_pe,
            min_pb=min_pb,
            max_pb=max_pb,
            min_dividend_yield=min_dividend_yield,
            include_etf=include_etf,
            etf_only=etf_only,
            limit=limit,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/factor-ranking", response_model=FactorRankingResponse)
async def factor_ranking(
    user: CurrentUser,
    db: Db,
    as_of: date | None = Query(None, description="Point-in-time ranking date"),
    profile: str = Query("balanced", pattern="^(balanced|value|momentum|defensive|income)$"),
    limit: int = Query(50, ge=5, le=200),
    sector_neutral: bool = Query(True),
    weight_source: str = Query("champion", pattern="^(champion|profile)$"),
):
    """Explainable point-in-time TW multi-factor ranking (not ML)."""
    try:
        champion = (
            await factor_registry_svc.get_champion(db, UUID(user["id"]), profile)
            if weight_source == "champion" else None
        )
        return await factor_svc.get_factor_ranking(
            as_of=as_of, profile=profile, limit=limit,
            sector_neutral=sector_neutral,
            **({
                "weights_override": champion.weights,
                "model_id": str(champion.id),
            } if champion else {}),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=502, detail="Factor ranking is temporarily unavailable")


@router.post("/factor-portfolio", response_model=FactorPortfolioResponse)
async def factor_portfolio(body: FactorPortfolioRequest, user: CurrentUser, db: Db):
    """Construct a non-executing, constrained portfolio from factor ranks."""
    anchor = body.as_of or date.today()
    try:
        champion = (
            await factor_registry_svc.get_champion(db, UUID(user["id"]), body.profile)
            if body.weight_source == "champion" else None
        )
        ranking = await factor_svc.get_factor_ranking(
            as_of=body.as_of, profile=body.profile, limit=body.candidate_count,
            sector_neutral=body.sector_neutral,
            **({
                "weights_override": champion.weights,
                "model_id": str(champion.id),
            } if champion else {}),
        )
        return await factor_portfolio_svc.construct_factor_portfolio(
            ranking=ranking, as_of=anchor, candidate_count=body.candidate_count,
            portfolio_notional_twd=body.portfolio_notional_twd,
            max_position_weight=body.max_position_weight,
            max_sector_weight=body.max_sector_weight,
            target_volatility=body.target_volatility,
            max_tracking_error=body.max_tracking_error,
            turnover_budget=body.turnover_budget,
            minimum_invested_weight=body.minimum_invested_weight,
            max_participation_rate=body.max_participation_rate,
            risk_aversion=body.risk_aversion,
            current_weights=body.current_weights,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=502, detail="Factor portfolio construction is unavailable")


@router.post("/factor-portfolio/rebalance-preview", response_model=FactorRebalancePreviewResponse)
async def factor_rebalance_preview(
    body: FactorRebalancePreviewRequest, user: CurrentUser, db: Db,
):
    """Owner-scoped factor rebalance preview. Never persists or executes trades."""
    from services.tw_factor_rebalance_service import build_factor_rebalance_preview

    anchor = date.today()
    try:
        if body.as_of is not None and body.as_of != anchor:
            raise ValueError(
                "historical as_of is unsupported for actual-holdings previews; "
                "historical holdings and cash snapshots are not available"
            )
        champion = (
            await factor_registry_svc.get_champion(db, UUID(user["id"]), body.profile)
            if body.weight_source == "champion" else None
        )
        ranking = await factor_svc.get_factor_ranking(
            as_of=body.as_of, profile=body.profile, limit=body.candidate_count,
            sector_neutral=body.sector_neutral,
            **({
                "weights_override": champion.weights,
                "model_id": str(champion.id),
            } if champion else {}),
        )
        options = body.model_dump(exclude={
            "portfolio_id", "as_of", "profile", "sector_neutral", "weight_source",
        })
        return await build_factor_rebalance_preview(
            portfolio_id=str(body.portfolio_id), user_id=user["id"], db=db,
            ranking=ranking, as_of=anchor, **options,
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if "not found" in detail.lower() else 400
        raise HTTPException(status_code=status_code, detail=detail)
    except Exception:
        raise HTTPException(status_code=502, detail="Factor rebalance preview is unavailable")


@router.get("/factor-validation", response_model=FactorValidationResponse)
async def factor_validation(
    _: CurrentUser,
    start_date: date = Query(...),
    end_date: date = Query(...),
    profile: str = Query("balanced", pattern="^(balanced|value|momentum|defensive|income)$"),
    top_n: int = Query(20, ge=5, le=100),
    holding_sessions: int = Query(21, ge=5, le=63),
    transaction_cost_bps: float = Query(20, ge=0, le=200),
    sector_neutral: bool = Query(True),
    portfolio_notional_twd: float = Query(10_000_000, ge=100_000, le=1_000_000_000),
    max_participation_rate: float = Query(0.05, gt=0, le=0.2),
    impact_coefficient_bps: float = Query(10, ge=0, le=100),
    benchmark: str = Query("taiex_total_return", pattern="^(taiex_total_return|equal_weight)$"),
    weight_mode: str = Query("walk_forward", pattern="^(fixed|walk_forward)$"),
):
    """Rolling forward-return validation with costs and explicit bias flags."""
    if (end_date - start_date).days > 5 * 366:
        raise HTTPException(status_code=400, detail="validation window cannot exceed 5 years")
    try:
        return await factor_svc.validate_factor_ranking(
            start_date=start_date, end_date=end_date, profile=profile,
            top_n=top_n, holding_sessions=holding_sessions,
            transaction_cost_bps=transaction_cost_bps,
            sector_neutral=sector_neutral,
            portfolio_notional_twd=portfolio_notional_twd,
            max_participation_rate=max_participation_rate,
            impact_coefficient_bps=impact_coefficient_bps,
            benchmark=benchmark,
            weight_mode=weight_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=502, detail="Factor validation is temporarily unavailable")


def _research_summary(row) -> dict:
    return {
        "id": row.id, "name": row.name, "profile": row.profile,
        "methodology_version": row.methodology_version,
        "parameters": row.parameters or {}, "summary": row.summary or {},
        "gate_result": row.gate_result or {}, "created_at": row.created_at,
    }


def _model_response(row) -> dict:
    return {
        "id": row.id, "profile": row.profile,
        "version_number": row.version_number,
        "methodology_version": row.methodology_version,
        "status": row.status, "weights": row.weights or {},
        "metrics": row.metrics or {}, "gate_result": row.gate_result or {},
        "source_run_id": row.source_run_id, "promoted_at": row.promoted_at,
        "promotion_note": row.promotion_note, "created_at": row.created_at,
    }


@router.post("/factor-research-runs", response_model=FactorResearchCreated)
async def create_factor_research_run(
    body: FactorResearchRequest, user: CurrentUser, db: Db,
):
    """Run, persist, gate, and register a reproducible factor challenger."""
    if body.start_date >= body.end_date:
        raise HTTPException(status_code=400, detail="start_date must be before end_date")
    if (body.end_date - body.start_date).days > 5 * 366:
        raise HTTPException(status_code=400, detail="validation window cannot exceed 5 years")
    try:
        result = await factor_svc.validate_factor_ranking(
            start_date=body.start_date, end_date=body.end_date, profile=body.profile,
            top_n=body.top_n, holding_sessions=body.holding_sessions,
            transaction_cost_bps=body.transaction_cost_bps,
            sector_neutral=body.sector_neutral,
            portfolio_notional_twd=body.portfolio_notional_twd,
            max_participation_rate=body.max_participation_rate,
            impact_coefficient_bps=body.impact_coefficient_bps,
            benchmark=body.benchmark, weight_mode=body.weight_mode,
        )
        run, model = await factor_registry_svc.save_research_result(
            db, user_id=UUID(user["id"]), name=body.name,
            parameters=body.model_dump(
                mode="json", exclude={"name", "auto_promote"},
            ),
            result=result, auto_promote=body.auto_promote,
        )
        return {"run": _research_summary(run), "model": _model_response(model)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="Factor research run could not be saved")


@router.get("/factor-research-runs", response_model=FactorResearchRunList)
async def list_factor_research_runs(
    user: CurrentUser, db: Db,
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
):
    rows, total = await factor_registry_svc.list_runs(
        db, UUID(user["id"]), limit=limit, offset=offset,
    )
    return {
        "items": [_research_summary(row) for row in rows],
        "total": total, "limit": limit, "offset": offset,
    }


@router.get("/factor-research-runs/{run_id}", response_model=FactorResearchRunDetail)
async def get_factor_research_run(run_id: UUID, user: CurrentUser, db: Db):
    row = await factor_registry_svc.get_run(db, UUID(user["id"]), run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Research run not found")
    return {**_research_summary(row), "result": row.result}


@router.get("/factor-models", response_model=list[FactorModelVersionResponse])
async def list_factor_models(
    user: CurrentUser, db: Db,
    profile: str | None = Query(
        None, pattern="^(balanced|value|momentum|defensive|income)$",
    ),
):
    rows = await factor_registry_svc.list_models(db, UUID(user["id"]), profile=profile)
    return [_model_response(row) for row in rows]


@router.post(
    "/factor-models/{model_id}/promote", response_model=FactorModelVersionResponse,
)
async def promote_factor_model(model_id: UUID, user: CurrentUser, db: Db):
    try:
        row = await factor_registry_svc.promote_model(
            db, user_id=UUID(user["id"]), model_id=model_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if row is None:
        raise HTTPException(status_code=404, detail="Factor model not found")
    return _model_response(row)


@router.get("/indices", response_model=TWIndexResponse)
async def indices(_: CurrentUser):
    """TAIEX 加權股價指數."""
    try:
        return await svc.get_index()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/industry/{symbol}")
async def industry(symbol: str, _: CurrentUser):
    """Return cached industry + name for a TW symbol.

    Backed by the in-memory `_industry_map` / `_name_map` populated
    daily by the `tw_symbol_map` cron from TWSE `t187ap03_L`. Empty
    fields when the symbol isn't recognised — frontend can fall
    back to "—" rather than 404 since the endpoint is informational
    enrichment, not a primary lookup.
    """
    sym = symbol.upper().strip()
    return {
        "symbol":   sym,
        "industry": svc.get_industry(sym),
        "name_zh":  svc.get_company_name(sym),
    }


@router.get("/news/recent")
async def news_recent(_: CurrentUser, limit: int = Query(20, ge=1, le=50)):
    """Market-wide TW news from the ingest archive — DB only, no live
    upstream fallback. Returns articles with `symbol IS NULL` (so per-
    symbol headlines don't crowd out broader market commentary) plus
    sentiment_score / sentiment_label for the dashboard's coloured
    badges. Empty list when the ingest task hasn't populated anything
    yet (fresh deploy, FinMind paywall window before PR #128, etc.)."""
    from services.ingest.repository import read_recent_news_autosession
    rows = await read_recent_news_autosession(
        "TW", symbol=None, limit=limit,
        max_age_days=7, include_sentiment=True,
    )
    return _quality_rows(rows, ["finmind_archive"])


@router.get("/news/{symbol}")
async def news(symbol: str, _: CurrentUser, limit: int = Query(10, le=30)):
    """Recent news headlines for a TW stock (via yfinance .TW suffix)."""
    return _quality_rows(
        await svc.get_news(symbol.upper(), limit=limit),
        ["finmind_archive", "google_news", "yfinance"],
    )


@router.get("/search")
async def search(
    _: CurrentUser,
    q: str = Query(..., min_length=1, max_length=20),
    limit: int = Query(10, ge=1, le=30),
):
    """Search TW exchange symbols by prefix or substring (case-insensitive)."""
    results = [
        {"symbol": sym, "market": "TW"}
        for sym in svc._exchange_map
        if q.upper() in sym.upper()
    ][:limit]
    return results
