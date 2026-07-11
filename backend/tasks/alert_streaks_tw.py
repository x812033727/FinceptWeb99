"""外資連 N 日買超 alert evaluation (PR-D1, TW-only daily rule).

`foreign_net_buy_streak` can't be evaluated on the quote tick — it
reads `tw_institutional_daily`, which lands once per trading day via
`ingest_institutional_tw` (06:50 UTC). This task runs 30 min after
that ingest and checks every active streak alert: the most recent
`days` institutional rows for the symbol must ALL be foreign
(fini_buy - fini_sell) net-buy days.

Firing goes through the same helpers as the tick path
(`fire_alert` / `dispatch_notifications`), so repeat/cooldown
semantics, the alert_events history row and the WS push are
identical. Cooldown notes: a repeat streak alert with cooldown 0
re-fires at most once per day — this task is the only evaluator.
"""
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import AsyncSessionLocal
from models.alert import PriceAlert
from models.tw_chip_metrics import TwInstitutionalDaily
from services.alert_rules import FireResult
from services.alert_service import cooldown_ok, dispatch_notifications, fire_alert

log = logging.getLogger(__name__)

MARKET = "TW"
CONDITION_TYPE = "foreign_net_buy_streak"


async def _foreign_net_buy_streak_days(
    db: AsyncSession, symbol: str, days: int,
) -> tuple[bool, list[int]]:
    """Whether the last `days` institutional rows are all foreign
    net-buy. Returns (matched, per-day net volumes newest-first)."""
    stmt = (
        select(TwInstitutionalDaily)
        .where(
            TwInstitutionalDaily.market == MARKET,
            TwInstitutionalDaily.symbol == symbol,
        )
        .order_by(TwInstitutionalDaily.ts.desc())
        .limit(days)
    )
    rows = (await db.scalars(stmt)).all()
    if len(rows) < days:
        return False, []
    nets: list[int] = []
    for r in rows:
        if r.fini_buy is None or r.fini_sell is None:
            return False, []
        nets.append(int(r.fini_buy) - int(r.fini_sell))
    return all(n > 0 for n in nets), nets


async def check_foreign_streak_alerts(db: AsyncSession) -> int:
    """Evaluate all active streak alerts; returns the fire count."""
    result = await db.execute(
        select(PriceAlert).where(
            PriceAlert.market == MARKET,
            PriceAlert.condition_type == CONDITION_TYPE,
            PriceAlert.triggered.is_(False),
        )
    )
    alerts = list(result.scalars().all())
    if not alerts:
        return 0

    now = datetime.now(UTC)
    fired: list[tuple[PriceAlert, FireResult]] = []
    streak_cache: dict[tuple[str, int], tuple[bool, list[int]]] = {}

    for alert in alerts:
        if not cooldown_ok(alert, now):
            continue
        days = int((alert.params or {}).get("days", 0))
        if days < 2:
            continue
        cache_key = (alert.symbol, days)
        if cache_key not in streak_cache:
            streak_cache[cache_key] = await _foreign_net_buy_streak_days(
                db, alert.symbol, days,
            )
        matched, nets = streak_cache[cache_key]
        if not matched:
            continue
        match = FireResult(
            message=(
                f"{alert.symbol} 外資連 {days} 日買超"
                f"(最近一日淨買超 {nets[0]:,} 股)"
            ),
            payload={"days": days, "net_buys": nets},
        )
        fire_alert(db, alert, match, now)
        fired.append((alert, match))

    if fired:
        await db.commit()
        await dispatch_notifications(fired)
    return len(fired)


async def run() -> None:
    """Scheduler entrypoint — 07:20 UTC daily, after the 06:50 UTC
    `ingest_institutional_tw` ingest has landed the day's rows."""
    try:
        async with AsyncSessionLocal() as db:
            fired = await check_foreign_streak_alerts(db)
        if fired:
            log.info("alert_streaks_tw: fired %d streak alert(s)", fired)
    except Exception as exc:
        log.warning("alert_streaks_tw failed: %s", exc)
