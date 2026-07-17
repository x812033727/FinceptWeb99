from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from models.decision_journal import DecisionJournalEntry
from models.discussion import Discussion
from services.ingest.repository import read_ohlcv_range_autosession
from services.tw_trading_calendar import to_tw_date

HORIZONS = {"d1": 0, "d5": 4, "d20": 19}
DEFAULT_TRANSACTION_COST_BPS = 15.0


def _recommendations(discussion: Discussion) -> list[dict[str, Any]]:
    conclusion = discussion.conclusion or {}
    structured = conclusion.get("recommendations")
    if isinstance(structured, list) and structured:
        return [row for row in structured if isinstance(row, dict) and row.get("symbol")]
    return [{"symbol": symbol, "confidence": 0.5} for symbol in conclusion.get("recommended_symbols") or []]


def _anchor(discussion: Discussion) -> date:
    return discussion.as_of_date or to_tw_date(discussion.created_at)


def _source_type(discussion: Discussion) -> str:
    if discussion.as_of_date is not None:
        return "backtest_recommendation"
    return "paper_recommendation" if discussion.auto_run else "research_recommendation"


def calculate_outcomes(bars: list[dict[str, Any]], transaction_cost_bps: float) -> dict[str, Any]:
    if not bars:
        return {"entry_price": None, "outcomes": {}, "max_drawdown_pct": None, "observations": 0, "status": "pending"}
    first_open = bars[0].get("open")
    if not isinstance(first_open, (int, float)) or first_open <= 0:
        return {"entry_price": None, "outcomes": {}, "max_drawdown_pct": None, "observations": len(bars), "status": "unavailable"}
    entry = float(first_open)
    outcomes: dict[str, Any] = {}
    cost_pct = transaction_cost_bps / 100
    for name, index in HORIZONS.items():
        close = bars[index].get("close") if len(bars) > index else None
        gross = (float(close) / entry - 1) * 100 if isinstance(close, (int, float)) else None
        outcomes[name] = {
            "close": float(close) if isinstance(close, (int, float)) else None,
            "gross_return_pct": round(gross, 4) if gross is not None else None,
            "net_return_pct": round(gross - cost_pct, 4) if gross is not None else None,
            "resolved": gross is not None,
        }
    closes = [float(row["close"]) for row in bars[:20] if isinstance(row.get("close"), (int, float))]
    peak = entry
    max_drawdown = 0.0
    for close in closes:
        peak = max(peak, close)
        max_drawdown = min(max_drawdown, (close / peak - 1) * 100)
    return {
        "entry_price": entry, "outcomes": outcomes,
        "max_drawdown_pct": round(max_drawdown, 4), "observations": len(closes),
        "status": "resolved" if outcomes["d20"]["resolved"] else "tracking",
    }


async def refresh_decision_journal(db: AsyncSession) -> int:
    # load_only: this walk touches every concluded discussion; without
    # it each row also materializes the big unused JSON columns
    # (post-mortems, outcome vectors, candidate snapshots).
    discussions = list((await db.scalars(
        select(Discussion)
        .options(load_only(
            Discussion.id,
            Discussion.owner_id,
            Discussion.market,
            Discussion.conclusion,
            Discussion.created_at,
            Discussion.as_of_date,
            Discussion.auto_run,
        ))
        .where(Discussion.conclusion.is_not(None))
    )).all())
    changed = 0
    for discussion in discussions:
        if discussion.market not in {"TW", "US"}:
            continue
        for recommendation in _recommendations(discussion):
            symbol = str(recommendation["symbol"]).strip().upper()
            if not symbol:
                continue
            source_type = _source_type(discussion)
            entry = await db.scalar(select(DecisionJournalEntry).where(
                DecisionJournalEntry.user_id == discussion.owner_id,
                DecisionJournalEntry.source_type == source_type,
                DecisionJournalEntry.source_id == str(discussion.id),
                DecisionJournalEntry.symbol == symbol,
            ))
            if entry is not None and entry.status == "resolved":
                continue
            anchor = _anchor(discussion)
            bars = await read_ohlcv_range_autosession(
                discussion.market, symbol, anchor, anchor + timedelta(days=45),
            )
            result = calculate_outcomes(bars[:20], DEFAULT_TRANSACTION_COST_BPS)
            confidence = recommendation.get("calibrated_confidence", recommendation.get("confidence"))
            if entry is None:
                entry = DecisionJournalEntry(
                    user_id=discussion.owner_id, source_type=source_type,
                    source_id=str(discussion.id), market=discussion.market, symbol=symbol,
                    prediction_at=discussion.created_at, anchor_date=anchor,
                )
                db.add(entry)
            entry.confidence = float(confidence) if isinstance(confidence, (int, float)) else None
            entry.entry_price = result["entry_price"]
            entry.outcomes = result["outcomes"]
            entry.max_drawdown_pct = result["max_drawdown_pct"]
            entry.transaction_cost_bps = DEFAULT_TRANSACTION_COST_BPS
            entry.observations = result["observations"]
            entry.status = result["status"]
            entry.updated_at = datetime.now(UTC)
            changed += 1

    # Daily AI candidates are persisted directly by daily_pick_service. They
    # share the exact same D1/D5/D20 outcome calculator as discussion picks,
    # but their source is the immutable daily run rather than a Discussion.
    pick_entries = list((await db.scalars(select(DecisionJournalEntry).where(
        DecisionJournalEntry.source_type == "ai_stock_pick",
        DecisionJournalEntry.status != "resolved",
    ))).all())
    for entry in pick_entries:
        bars = await read_ohlcv_range_autosession(
            entry.market, entry.symbol,
            entry.anchor_date, entry.anchor_date + timedelta(days=45),
        )
        result = calculate_outcomes(bars[:20], entry.transaction_cost_bps)
        entry.entry_price = result["entry_price"]
        entry.outcomes = result["outcomes"]
        entry.max_drawdown_pct = result["max_drawdown_pct"]
        entry.observations = result["observations"]
        entry.status = result["status"]
        entry.updated_at = datetime.now(UTC)
        changed += 1
    await db.commit()
    return changed


async def list_entries(db: AsyncSession, user_id, *, limit: int = 100) -> list[DecisionJournalEntry]:
    rows = await db.scalars(select(DecisionJournalEntry).where(
        DecisionJournalEntry.user_id == user_id,
    ).order_by(DecisionJournalEntry.prediction_at.desc()).limit(limit))
    return list(rows)


def summarize(entries: list[DecisionJournalEntry]) -> dict[str, Any]:
    summary: dict[str, Any] = {"sample_size": len(entries), "resolved_d20": 0, "horizons": {}}
    for horizon in HORIZONS:
        values = [
            row.outcomes[horizon]["net_return_pct"] for row in entries
            if row.outcomes.get(horizon, {}).get("resolved")
        ]
        summary["horizons"][horizon] = {
            "sample_size": len(values),
            "win_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100, 2) if values else None,
            "average_net_return_pct": round(sum(values) / len(values), 4) if values else None,
        }
    summary["resolved_d20"] = summary["horizons"]["d20"]["sample_size"]
    drawdowns = [row.max_drawdown_pct for row in entries if row.max_drawdown_pct is not None]
    summary["average_max_drawdown_pct"] = round(sum(drawdowns) / len(drawdowns), 4) if drawdowns else None
    return summary
