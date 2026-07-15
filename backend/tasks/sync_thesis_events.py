"""Project newly archived research events into owner-scoped thesis timelines."""
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import AsyncSessionLocal
from models.corporate_announcement import CorporateAnnouncement
from models.investment_thesis import InvestmentThesis, ThesisEvent
from models.news_article import NewsArticle
from models.tw_chip_metrics import TwInstitutionalDaily
from models.tw_revenue_monthly import TwRevenueMonthly

log = logging.getLogger(__name__)

_METRIC_FIELDS = {
    "revenue_yoy_pct": ("revenue", "yoy_pct"),
    "revenue_mom_pct": ("revenue", "mom_pct"),
    "foreign_net": ("institutional", "foreign_net"),
}


def _condition_key(condition: dict) -> str:
    configured = str(condition.get("id") or "").strip()
    if configured:
        return configured[:64]
    payload = json.dumps(condition, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _condition_matches(condition: dict, event: ThesisEvent) -> tuple[bool, float | None]:
    mapping = _METRIC_FIELDS.get(condition.get("metric"))
    if mapping is None or event.event_type != mapping[0]:
        return False, None
    observed = event.details.get(mapping[1])
    try:
        observed_value = float(observed)
        threshold = float(condition["threshold"])
    except (KeyError, TypeError, ValueError):
        return False, None
    operator = condition.get("operator")
    matched = {
        "lt": observed_value < threshold,
        "lte": observed_value <= threshold,
        "gt": observed_value > threshold,
        "gte": observed_value >= threshold,
        "eq": observed_value == threshold,
    }.get(operator, False)
    return matched, observed_value


async def _evaluate_watch_conditions(
    db: AsyncSession,
    thesis: InvestmentThesis,
    *,
    cutoff_dt: datetime,
) -> int:
    conditions = [item for item in (thesis.watch_conditions or []) if isinstance(item, dict)]
    if not conditions:
        return 0

    await db.flush()
    evidence_events = list((await db.scalars(select(ThesisEvent).where(
        ThesisEvent.thesis_id == thesis.id,
        ThesisEvent.occurred_at >= cutoff_dt,
        ThesisEvent.event_type.in_(("revenue", "institutional")),
    ))).all())
    known_refs = await _known_refs(db, thesis.id)
    added = 0
    for condition in conditions:
        key = _condition_key(condition)
        for evidence in evidence_events:
            matched, observed = _condition_matches(condition, evidence)
            ref = f"watch:{key}:{evidence.id}"
            if not matched or ref in known_refs:
                continue
            label = str(condition.get("label") or condition.get("metric") or "Watch condition")
            db.add(ThesisEvent(
                thesis_id=thesis.id,
                user_id=thesis.user_id,
                event_type="watch_condition_triggered",
                title=f"Watch condition triggered: {label}"[:240],
                details={
                    "condition": condition,
                    "observed_value": observed,
                    "evidence_event_id": str(evidence.id),
                    "evidence_source_ref": evidence.source_ref,
                },
                source="thesis_watch_engine",
                source_ref=ref,
                occurred_at=evidence.occurred_at,
            ))
            known_refs.add(ref)
            added += 1
    return added


async def _known_refs(db: AsyncSession, thesis_id) -> set[str]:
    rows = await db.scalars(select(ThesisEvent.source_ref).where(
        ThesisEvent.thesis_id == thesis_id, ThesisEvent.source_ref.is_not(None),
    ))
    return set(rows)


async def sync_thesis_events(db: AsyncSession, *, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    cutoff_dt = now - timedelta(days=35)
    cutoff_date = cutoff_dt.date()
    theses = list((await db.scalars(select(InvestmentThesis).where(
        InvestmentThesis.status.in_(("active", "watching")),
    ))).all())
    added = 0

    for thesis in theses:
        known = await _known_refs(db, thesis.id)

        announcements = (await db.scalars(select(CorporateAnnouncement).where(
            CorporateAnnouncement.market == thesis.market,
            CorporateAnnouncement.symbol == thesis.symbol,
            CorporateAnnouncement.announced_at >= cutoff_dt,
        ).order_by(CorporateAnnouncement.announced_at).limit(50))).all()
        for row in announcements:
            ref = f"announcement:{row.id}"
            if ref not in known:
                db.add(ThesisEvent(
                    thesis_id=thesis.id, user_id=thesis.user_id,
                    event_type="announcement", title=row.title[:240],
                    details={"category": row.category, "sentiment": row.sentiment_label},
                    source=row.source, source_ref=ref, occurred_at=row.announced_at,
                ))
                known.add(ref)
                added += 1

        news = (await db.scalars(select(NewsArticle).where(
            NewsArticle.market == thesis.market,
            NewsArticle.symbol == thesis.symbol,
            NewsArticle.published_at >= cutoff_dt,
        ).order_by(NewsArticle.published_at).limit(50))).all()
        for row in news:
            ref = f"news:{row.id}"
            if ref not in known:
                db.add(ThesisEvent(
                    thesis_id=thesis.id, user_id=thesis.user_id,
                    event_type="news", title=row.title[:240],
                    details={"publisher": row.publisher, "sentiment": row.sentiment_label},
                    source=row.source, source_ref=ref, occurred_at=row.published_at,
                ))
                known.add(ref)
                added += 1

        if thesis.market == "TW":
            revenues = (await db.scalars(select(TwRevenueMonthly).where(
                TwRevenueMonthly.market == thesis.market,
                TwRevenueMonthly.symbol == thesis.symbol,
                TwRevenueMonthly.ts >= cutoff_date,
            ).order_by(TwRevenueMonthly.ts).limit(3))).all()
            for row in revenues:
                ref = f"revenue:{row.market}:{row.symbol}:{row.ts.isoformat()}"
                if ref not in known:
                    db.add(ThesisEvent(
                        thesis_id=thesis.id, user_id=thesis.user_id,
                        event_type="revenue", title=f"{row.symbol} monthly revenue update",
                        details={"revenue": row.revenue, "yoy_pct": float(row.revenue_yoy) if row.revenue_yoy is not None else None, "mom_pct": float(row.revenue_mom) if row.revenue_mom is not None else None},
                        source=row.source, source_ref=ref,
                        occurred_at=datetime.combine(row.ts, datetime.min.time(), tzinfo=UTC),
                    ))
                    known.add(ref)
                    added += 1

            institutional = (await db.scalars(select(TwInstitutionalDaily).where(
                TwInstitutionalDaily.market == thesis.market,
                TwInstitutionalDaily.symbol == thesis.symbol,
                TwInstitutionalDaily.ts >= cutoff_date,
            ).order_by(TwInstitutionalDaily.ts).limit(35))).all()
            for row in institutional:
                ref = f"institutional:{row.market}:{row.symbol}:{row.ts.isoformat()}"
                if ref not in known:
                    foreign_net = None
                    if row.fini_buy is not None and row.fini_sell is not None:
                        foreign_net = row.fini_buy - row.fini_sell
                    db.add(ThesisEvent(
                        thesis_id=thesis.id, user_id=thesis.user_id,
                        event_type="institutional", title=f"{row.symbol} institutional flow update",
                        details={"foreign_net": foreign_net}, source=row.source, source_ref=ref,
                        occurred_at=datetime.combine(row.ts, datetime.min.time(), tzinfo=UTC),
                    ))
                    known.add(ref)
                    added += 1

        added += await _evaluate_watch_conditions(db, thesis, cutoff_dt=cutoff_dt)

    await db.commit()
    return added


async def run() -> None:
    try:
        async with AsyncSessionLocal() as db:
            added = await sync_thesis_events(db)
        if added:
            log.info("sync_thesis_events: added %d event(s)", added)
    except Exception:
        log.exception("sync_thesis_events failed")
