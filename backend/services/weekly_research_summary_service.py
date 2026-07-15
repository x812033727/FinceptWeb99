import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.alert import AlertEvent
from models.decision_journal import DecisionJournalEntry
from models.investment_thesis import InvestmentThesis, ThesisEvent
from models.portfolio import Portfolio, PortfolioSnapshot
from models.stock_report import StockReport
from models.stock_pick_run import StockPickRun


def _utc_iso(value: datetime) -> str:
    """Keep timestamps unambiguous across PostgreSQL and SQLite tests."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


async def build_weekly_summary(
    db: AsyncSession, user_id: uuid.UUID, *, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=7)

    theses = list((await db.scalars(select(InvestmentThesis).where(
        InvestmentThesis.user_id == user_id,
    ))).all())
    thesis_events = list((await db.scalars(select(ThesisEvent).where(
        ThesisEvent.user_id == user_id, ThesisEvent.occurred_at >= since,
    ).order_by(ThesisEvent.occurred_at.desc()))).all())
    alerts = list((await db.scalars(select(AlertEvent).where(
        AlertEvent.user_id == user_id, AlertEvent.fired_at >= since,
    ).order_by(AlertEvent.fired_at.desc()))).all())
    reports = list((await db.scalars(select(StockReport).where(
        StockReport.user_id == user_id, StockReport.created_at >= since,
    ))).all())
    pick_runs = list((await db.scalars(select(StockPickRun).where(
        StockPickRun.user_id == user_id,
        StockPickRun.generated_at >= since,
    ))).all())
    decisions = list((await db.scalars(select(DecisionJournalEntry).where(
        DecisionJournalEntry.user_id == user_id,
    ))).all())

    event_counts = Counter(event.event_type for event in thesis_events)
    due = [
        {"id": str(thesis.id), "market": thesis.market, "symbol": thesis.symbol,
         "title": thesis.title, "review_date": thesis.review_date.isoformat()}
        for thesis in theses
        if thesis.status in {"active", "watching"}
        and thesis.review_date is not None and thesis.review_date <= now.date()
    ]
    tracking = [
        {"id": str(row.id), "market": row.market, "symbol": row.symbol,
         "observations": row.observations, "status": row.status}
        for row in decisions if row.status != "resolved"
    ]
    watch_triggers = [
        {
            "thesis_id": str(row.thesis_id),
            "title": row.title,
            "condition": row.details.get("condition", {}),
            "observed_value": row.details.get("observed_value"),
            "occurred_at": _utc_iso(row.occurred_at),
        }
        for row in thesis_events
        if row.event_type == "watch_condition_triggered"
    ]

    calibration_rows = []
    for row in decisions:
        d5 = row.outcomes.get("d5", {})
        if row.confidence is None or not d5.get("resolved"):
            continue
        outcome = 1.0 if float(d5["net_return_pct"]) > 0 else 0.0
        calibration_rows.append((float(row.confidence) - outcome) ** 2)

    portfolios = list((await db.scalars(select(Portfolio).where(
        Portfolio.user_id == user_id,
    ))).all())
    portfolio_ids = [row.id for row in portfolios]
    snapshots = []
    if portfolio_ids:
        snapshots = list((await db.scalars(select(PortfolioSnapshot).where(
            PortfolioSnapshot.portfolio_id.in_(portfolio_ids),
            PortfolioSnapshot.snapshot_date >= since.date(),
        ).order_by(PortfolioSnapshot.snapshot_date))).all())
    by_portfolio: dict[uuid.UUID, list[PortfolioSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        by_portfolio[snapshot.portfolio_id].append(snapshot)
    names = {row.id: row.name for row in portfolios}
    portfolio_drift = []
    for portfolio_id, rows in by_portfolio.items():
        first, last = rows[0], rows[-1]
        start = float(first.total_value_usd)
        end = float(last.total_value_usd)
        portfolio_drift.append({
            "portfolio_id": str(portfolio_id), "name": names[portfolio_id],
            "start_value_usd": round(start, 2), "end_value_usd": round(end, 2),
            "change_pct": round((end / start - 1) * 100, 4) if start else None,
            "observations": len(rows),
        })

    return {
        "period": {"from": since.isoformat(), "to": now.isoformat()},
        "theses": {
            "active": sum(row.status in {"active", "watching"} for row in theses),
            "events": len(thesis_events), "event_counts": dict(event_counts),
            "recent": [
                {"thesis_id": str(row.thesis_id), "type": row.event_type,
                "title": row.title, "occurred_at": _utc_iso(row.occurred_at)}
                for row in thesis_events[:20]
            ],
        },
        "alerts": {"count": len(alerts), "by_kind": dict(Counter(row.kind for row in alerts))},
        "portfolio_drift": portfolio_drift,
        "ai": {
            "reports_generated": len(reports),
            "average_report_quality": round(sum(row.quality_score for row in reports) / len(reports), 4) if reports else None,
            "calibration_sample_size": len(calibration_rows),
            "d5_brier_score": round(sum(calibration_rows) / len(calibration_rows), 4) if calibration_rows else None,
            "daily_pick_runs": len(pick_runs),
            "daily_candidates": sum(row.candidate_count for row in pick_runs),
        },
        "pending": {
            "thesis_reviews": due,
            "decision_outcomes": tracking,
            "watch_triggers": watch_triggers,
        },
        "disclaimer": "Research workflow summary only; not investment advice.",
    }
