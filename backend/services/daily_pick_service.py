from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.decision_journal import DecisionJournalEntry
from models.stock_pick_run import StockPickRun
from models.stock_report import StockReport
from middleware.metrics import DAILY_PICK_CANDIDATES_TOTAL, DAILY_PICK_RUNS_TOTAL

METHODOLOGY_VERSION = "trusted-report-ranking-v1"
LOOKBACK_DAYS = 7
MIN_REPORT_QUALITY = 0.70
MAX_CANDIDATES = 5


class NoEligibleCandidatesError(ValueError):
    pass


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _conclusion(report: StockReport) -> str:
    if isinstance(report.sections, dict):
        value = report.sections.get("結論")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return report.content_md[-800:].strip()


def _stance(text: str) -> str:
    # Evaluate the conclusion only. Bearish wins if a malformed conclusion
    # contains both labels so it can never slip into a long candidate list.
    if "偏空" in text:
        return "bearish"
    if "偏多" in text:
        return "bullish"
    return "neutral"


def _candidate(report: StockReport, *, now: datetime) -> dict[str, Any] | None:
    conclusion = _conclusion(report)
    if _stance(conclusion) != "bullish" or report.quality_score < MIN_REPORT_QUALITY:
        return None
    age_days = max(0.0, (_aware(now) - _aware(report.created_at)).total_seconds() / 86400)
    if age_days > LOOKBACK_DAYS:
        return None
    recency = max(0.0, 1.0 - age_days / LOOKBACK_DAYS)
    score = round(report.quality_score * 60 + 25 + recency * 15, 2)
    snapshot = report.context_snapshot if isinstance(report.context_snapshot, dict) else {}
    quality = snapshot.get("quality_summary") if isinstance(snapshot.get("quality_summary"), dict) else {}
    evidence = [
        {
            key: item.get(key)
            for key in ("id", "path", "value", "source", "as_of")
        }
        for item in (report.evidence or [])[:8]
        if isinstance(item, dict)
    ]
    return {
        "symbol": report.symbol,
        "market": report.market,
        "score": score,
        "stance": "bullish",
        "confidence": round(min(0.95, max(0.5, score / 100)), 4),
        "rationale": conclusion[:500],
        "source_report_id": str(report.id),
        "report_quality": round(float(report.quality_score), 4),
        "report_created_at": _aware(report.created_at).isoformat(),
        "quality_details": quality,
        "evidence": evidence,
    }


async def generate_daily_picks(
    db: AsyncSession,
    user_id: uuid.UUID,
    market: str,
    *,
    now: datetime | None = None,
) -> StockPickRun:
    now = _aware(now or datetime.now(UTC))
    market = market.upper()
    if market not in {"TW", "US"}:
        raise ValueError("market must be TW or US")
    market_date = now.astimezone(
        ZoneInfo("Asia/Taipei" if market == "TW" else "America/New_York"),
    ).date()
    existing = await db.scalar(select(StockPickRun).where(
        StockPickRun.user_id == user_id,
        StockPickRun.market == market,
        StockPickRun.run_date == market_date,
    ))
    if existing is not None:
        DAILY_PICK_RUNS_TOTAL.labels(market=market.lower(), outcome="reused").inc()
        return existing

    reports = list((await db.scalars(select(StockReport).where(
        StockReport.user_id == user_id,
        StockReport.market == market,
        StockReport.created_at >= now - timedelta(days=LOOKBACK_DAYS),
    ).order_by(StockReport.created_at.desc()))).all())
    latest_by_symbol: dict[str, StockReport] = {}
    for report in reports:
        latest_by_symbol.setdefault(report.symbol, report)
    candidates = [
        item for report in latest_by_symbol.values()
        if (item := _candidate(report, now=now)) is not None
    ]
    candidates.sort(key=lambda item: (-item["score"], item["symbol"]))
    candidates = candidates[:MAX_CANDIDATES]
    if not candidates:
        DAILY_PICK_RUNS_TOTAL.labels(market=market.lower(), outcome="empty").inc()
        raise NoEligibleCandidatesError(
            "No eligible bullish reports in the last 7 days with quality >= 70%",
        )
    for rank, item in enumerate(candidates, start=1):
        item["rank"] = rank

    run = StockPickRun(
        user_id=user_id,
        market=market,
        run_date=market_date,
        methodology_version=METHODOLOGY_VERSION,
        status="completed",
        candidate_count=len(candidates),
        candidates=candidates,
        source_report_ids=[item["source_report_id"] for item in candidates],
        generated_at=now,
    )
    db.add(run)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raced = await db.scalar(select(StockPickRun).where(
            StockPickRun.user_id == user_id,
            StockPickRun.market == market,
            StockPickRun.run_date == market_date,
        ))
        if raced is None:
            raise
        DAILY_PICK_RUNS_TOTAL.labels(market=market.lower(), outcome="reused").inc()
        return raced
    # Prediction is timestamped after the report-based ranking and enters at
    # the first available session on/after tomorrow, preventing look-ahead.
    anchor = market_date + timedelta(days=1)
    for item in candidates:
        db.add(DecisionJournalEntry(
            user_id=user_id,
            source_type="ai_stock_pick",
            source_id=str(run.id),
            market=market,
            symbol=item["symbol"],
            prediction_at=now,
            anchor_date=anchor,
            stance="long",
            confidence=item["confidence"],
            status="pending",
        ))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raced = await db.scalar(select(StockPickRun).where(
            StockPickRun.user_id == user_id,
            StockPickRun.market == market,
            StockPickRun.run_date == market_date,
        ))
        if raced is None:
            raise
        DAILY_PICK_RUNS_TOTAL.labels(market=market.lower(), outcome="reused").inc()
        return raced
    await db.refresh(run)
    DAILY_PICK_RUNS_TOTAL.labels(market=market.lower(), outcome="completed").inc()
    DAILY_PICK_CANDIDATES_TOTAL.labels(market=market.lower()).inc(len(candidates))
    return run


async def list_pick_runs(
    db: AsyncSession, user_id: uuid.UUID, *, limit: int = 20,
) -> list[StockPickRun]:
    rows = await db.scalars(select(StockPickRun).where(
        StockPickRun.user_id == user_id,
    ).order_by(StockPickRun.generated_at.desc()).limit(limit))
    return list(rows)


async def latest_pick_runs(db: AsyncSession, user_id: uuid.UUID) -> list[StockPickRun]:
    rows = await list_pick_runs(db, user_id, limit=50)
    latest: dict[str, StockPickRun] = {}
    for row in rows:
        latest.setdefault(row.market, row)
    return [latest[key] for key in sorted(latest)]
