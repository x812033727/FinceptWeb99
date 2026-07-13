from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin
from db.session import get_db

from ..schemas import (
    SignalAuditHistoryOut,
    SignalAuditHistoryPoint,
    SignalAuditOut,
    SignalCoverageRow,
    SignalHallucinationRow,
)

router = APIRouter()
AdminUser = Annotated[dict, Depends(require_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]


# ── Signal-citation audit (PR #239-#240 surface) ─────────────────


_SIGNAL_AUDIT_RECENT_MAX = 200


@router.get("/signal-audit", response_model=SignalAuditOut)
async def signal_audit(
    _: AdminUser, db: DB,
    recent: int = 30,
    market: str | None = None,
) -> SignalAuditOut:
    """Run the same bulk audit as `python -m scripts.audit_signal_usage
    --recent N` and return the result as JSON for the AdminPage UI.

    `recent` is bounded at `_SIGNAL_AUDIT_RECENT_MAX` so a typo'd
    request can't fan out across the entire archive (each audited
    discussion = 2 SQL reads + per-turn regex scan; bulk over thousands
    of rows would take seconds + saturate the connection pool).

    `market` filters concluded discussions by market when set; default
    is all-markets.

    Coverage rows are pre-sorted ascending by `citation_rate` so the
    UI table renders zero-uptake offenders at the top without any
    client-side sort logic.
    """
    from services.signal_audit_service import audit_recent_discussions

    if recent < 1 or recent > _SIGNAL_AUDIT_RECENT_MAX:
        raise HTTPException(
            400,
            f"recent must be in [1, {_SIGNAL_AUDIT_RECENT_MAX}]; got {recent}",
        )

    summary = await audit_recent_discussions(
        db, limit=recent, market=market,
    )

    coverage_rows = []
    for sig, stats in summary.coverage.items():
        denom = stats["persona_count"]
        rate = (stats["cited"] / denom) if denom else 0.0
        cited_with_value = stats.get("cited_with_value", 0)
        value_rate = (cited_with_value / denom) if denom else 0.0
        coverage_rows.append(SignalCoverageRow(
            signal=sig,
            present=stats["present"],
            cited=stats["cited"],
            cited_with_value=cited_with_value,
            persona_count=denom,
            citation_rate=round(rate, 4),
            value_citation_rate=round(value_rate, 4),
        ))
    coverage_rows.sort(key=lambda r: (r.citation_rate, r.signal))

    hallucinations = []
    for sig, stats in summary.hallucinations.items():
        if stats["hallucinated"] <= 0:
            continue
        denom = stats["persona_count_absent"]
        rate = (stats["hallucinated"] / denom) if denom else 0.0
        hallucinations.append(SignalHallucinationRow(
            signal=sig,
            absent_rounds=stats["absent_rounds"],
            hallucinated=stats["hallucinated"],
            persona_count_absent=denom,
            hallucination_rate=round(rate, 4),
        ))
    hallucinations.sort(
        key=lambda r: (-r.hallucination_rate, r.signal),
    )

    zero_uptake = sorted(
        sig for sig, stats in summary.coverage.items()
        if stats["cited"] == 0 and stats["persona_count"] > 0
    )

    # PR #263: include per-signal trend history for the sparkline
    # column. Bulk read in one query so the frontend doesn't fan out
    # to N per-signal calls. Failures here are non-fatal — sparkline
    # is decorative.
    history: dict[str, list[SignalAuditHistoryPoint]] = {}
    try:
        from services.signal_audit_service import read_all_signals_history

        bulk = await read_all_signals_history(
            db, market=market, days=30,
        )
        history = {
            sig: [SignalAuditHistoryPoint(**p) for p in points]
            for sig, points in bulk.items()
        }
    except Exception:
        # Decorative — main response still useful without it.
        history = {}

    return SignalAuditOut(
        discussions_audited=summary.discussions_audited,
        discussion_ids=summary.discussion_ids,
        coverage=coverage_rows,
        zero_uptake=zero_uptake,
        hallucinations=hallucinations,
        history=history,
    )


# ── Signal-audit history sparkline (PR #263) ─────────────────────


_SIGNAL_AUDIT_HISTORY_DAYS_MAX = 365


@router.get(
    "/signal-audit-history",
    response_model=SignalAuditHistoryOut,
)
async def signal_audit_history(
    _: AdminUser, db: DB,
    signal: str,
    market: str | None = None,
    days: int = 30,
) -> SignalAuditHistoryOut:
    """Per-signal daily snapshot timeseries persisted by the
    `snapshot_signal_audit` cron. Supports the AdminPage sparkline
    column on the SignalAuditCard so admins can see whether a
    signal's citation rate is improving or deteriorating across
    deploys.

    `days` is bounded — pulling years of per-signal history at
    O(N rows × M signals) costs nothing on Postgres but slows the
    frontend; the bound keeps the JSON payload small.
    """
    from services.signal_audit_service import read_signal_history

    if days < 1 or days > _SIGNAL_AUDIT_HISTORY_DAYS_MAX:
        raise HTTPException(
            400,
            f"days must be in [1, {_SIGNAL_AUDIT_HISTORY_DAYS_MAX}]; "
            f"got {days}",
        )
    if not signal:
        raise HTTPException(400, "signal query param is required")

    points_data = await read_signal_history(
        db, signal=signal, market=market, days=days,
    )
    return SignalAuditHistoryOut(
        signal=signal,
        market=market,
        points=[SignalAuditHistoryPoint(**p) for p in points_data],
    )
