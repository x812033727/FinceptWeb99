"""
Alert CRUD + check-and-fire rule engine called from market refresh tasks.

PR-D1: `check_and_fire` grew from a fixed above/below comparator into
a registry-driven rule engine (`services/alert_rules.py`). Per tick it:

1. loads untriggered alerts for (symbol, market),
2. resolves any daily thresholds the batch needs (N-day high/low/均量
   from `ohlcv_daily`, cached in Redis per symbol per day so the hot
   tick path costs zero extra DB queries after the first),
3. runs each alert's evaluator against the quote tick,
4. applies firing semantics: repeat=False → fire once + disable
   (original behavior); repeat=True → re-fire after cooldown_seconds,

then writes the alert_events history row (same transaction as the
flag flip, PR-D5) and pushes the WS notification.

Daily condition types (foreign_net_buy_streak) are skipped here and
evaluated by `tasks/alert_streaks_tw.py` after institutional ingest,
via the shared `fire_alert` / `dispatch_notifications` helpers.
"""
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cache.cache_ttls import TTL_ALERT_THRESHOLD
from cache.redis_cache import cache_get_json, cache_set_json
from models.alert import AlertEvent, PriceAlert
from models.ohlcv_daily import OhlcvDaily
from schemas.alert import AlertCreate, AlertUpdate, validate_rule_fields
from services.alert_rules import (
    DAILY_CONDITION_TYPES,
    THR_AVG_VOL,
    THR_HIGH,
    THR_LOW,
    TICK_EVALUATORS,
    FireResult,
    TickContext,
    threshold_needs,
)
from services.notification_service import notify_user


def cooldown_ok(alert: PriceAlert, now: datetime) -> bool:
    """Whether firing is allowed now under repeat/cooldown semantics.
    (Once-only alerts are filtered upstream by `triggered == False`;
    this only gates the repeat re-fire path.)"""
    if not alert.repeat:
        return not alert.triggered
    if alert.last_fired_at is None:
        return True
    last = alert.last_fired_at
    if last.tzinfo is None:  # SQLite loses tzinfo
        last = last.replace(tzinfo=UTC)
    return now >= last + timedelta(seconds=alert.cooldown_seconds or 0)


def fire_alert(
    db: AsyncSession,
    alert: PriceAlert,
    result: FireResult,
    now: datetime,
) -> None:
    """Apply one firing: flip flags per repeat semantics and stage the
    alert_events history row in the CALLER's transaction (history and
    flag flip commit atomically, PR-D5). Caller commits + notifies."""
    alert.last_fired_at = now
    if not alert.repeat:
        alert.triggered = True
        alert.triggered_at = now
    db.add(AlertEvent(
        user_id=alert.user_id,
        alert_id=alert.id,
        symbol=alert.symbol,
        market=alert.market,
        kind="price",
        message=result.message,
        fired_at=now,
        payload={"condition_type": alert.condition_type, **result.payload},
    ))


async def dispatch_notifications(
    fired: list[tuple[PriceAlert, FireResult]],
) -> None:
    """WS push for every firing — after commit, so a transport error
    can't roll back the history row."""
    for alert, result in fired:
        await notify_user(str(alert.user_id), {
            "type": "alert",
            "id": str(alert.id),
            "symbol": alert.symbol,
            "market": alert.market,
            "condition_type": alert.condition_type,
            "message": result.message,
            "condition": alert.condition.value if alert.condition else None,
            "target_price": alert.target_price,
            **result.payload,
        })


class AlertService:
    @staticmethod
    async def list(db: AsyncSession, user_id: uuid.UUID) -> list[PriceAlert]:
        result = await db.execute(
            select(PriceAlert)
            .where(PriceAlert.user_id == user_id)
            .order_by(PriceAlert.created_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def create(db: AsyncSession, user_id: uuid.UUID, body: AlertCreate) -> PriceAlert:
        alert = PriceAlert(
            user_id=user_id,
            symbol=body.symbol.upper(),
            market=body.market,
            condition=body.condition,
            target_price=body.target_price,
            condition_type=body.condition_type,
            params=body.params,
            cooldown_seconds=body.cooldown_seconds,
            repeat=body.repeat,
        )
        db.add(alert)
        await db.commit()
        await db.refresh(alert)
        return alert

    @staticmethod
    async def update(
        db: AsyncSession,
        user_id: uuid.UUID,
        alert_id: uuid.UUID,
        body: AlertUpdate,
    ) -> PriceAlert | None:
        """Partial update of rule knobs. Returns None when the alert
        doesn't exist / belongs to another user. Raises ValueError on
        params/target_price invalid for the alert's condition_type
        (router maps to 422)."""
        result = await db.execute(
            select(PriceAlert).where(
                and_(PriceAlert.id == alert_id, PriceAlert.user_id == user_id)
            )
        )
        alert = result.scalar_one_or_none()
        if not alert:
            return None

        fields = body.model_dump(exclude_unset=True)
        if "params" in fields or "target_price" in fields:
            new_target = fields.get("target_price", alert.target_price)
            new_params = fields.get("params", alert.params)
            alert.params = validate_rule_fields(
                alert.condition_type, new_params,
                market=alert.market, target_price=new_target,
            )
            alert.target_price = new_target
        if "cooldown_seconds" in fields and fields["cooldown_seconds"] is not None:
            alert.cooldown_seconds = fields["cooldown_seconds"]
        if "repeat" in fields and fields["repeat"] is not None:
            alert.repeat = fields["repeat"]

        await db.commit()
        await db.refresh(alert)
        return alert

    @staticmethod
    async def delete(db: AsyncSession, user_id: uuid.UUID, alert_id: uuid.UUID) -> bool:
        result = await db.execute(
            select(PriceAlert).where(
                and_(PriceAlert.id == alert_id, PriceAlert.user_id == user_id)
            )
        )
        alert = result.scalar_one_or_none()
        if not alert:
            return False
        await db.delete(alert)
        await db.commit()
        return True

    @staticmethod
    async def history(
        db: AsyncSession,
        user_id: uuid.UUID,
        *,
        limit: int = 50,
        before: datetime | None = None,
    ) -> "list[AlertEvent]":  # quoted: AlertService.list shadows builtin list here
        """User-scoped fired-alert history, newest first. `before` is a
        `fired_at` cursor: pass the last row's fired_at to get the next
        page (strictly older rows)."""
        stmt = select(AlertEvent).where(AlertEvent.user_id == user_id)
        if before is not None:
            stmt = stmt.where(AlertEvent.fired_at < before)
        stmt = stmt.order_by(AlertEvent.fired_at.desc()).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def check_and_fire(
        db: AsyncSession,
        symbol: str,
        market: str,
        current_price: float,
        quote: dict | None = None,
    ) -> None:
        """
        Evaluate untriggered alerts for this symbol+market against the
        current tick, fire any that match. `quote` is the normalized
        quote payload from the refresh task (change_pct / volume feed
        the pct_change / volume_surge evaluators); omitting it keeps
        the pre-D1 price-only call signature working — price rules
        still evaluate, quote-dependent rules abstain.
        Pushes alert notifications via the registered notification
        transport (WebSocket in production).
        """
        result = await db.execute(
            select(PriceAlert).where(
                and_(
                    PriceAlert.symbol == symbol,
                    PriceAlert.market == market,
                    # repeat=True rules never flip `triggered`, so this
                    # single filter covers both firing semantics.
                    PriceAlert.triggered.is_(False),
                )
            )
        )
        alerts = [
            a for a in result.scalars().all()
            if a.condition_type not in DAILY_CONDITION_TYPES
        ]
        if not alerts:
            return

        quote = quote or {}
        ctx = TickContext(
            price=current_price,
            change_pct=quote.get("change_pct"),
            volume=quote.get("volume"),
            thresholds=await _resolve_thresholds(
                db, market, symbol, threshold_needs(alerts),
            ),
        )

        now = datetime.now(UTC)
        fired: list[tuple[PriceAlert, FireResult]] = []
        for alert in alerts:
            evaluator = TICK_EVALUATORS.get(alert.condition_type)
            if evaluator is None or not cooldown_ok(alert, now):
                continue
            match = evaluator(alert, ctx)
            if match is None:
                continue
            fire_alert(db, alert, match, now)
            fired.append((alert, match))

        if fired:
            await db.commit()
            await dispatch_notifications(fired)


# ── daily threshold resolution (breakout / volume_surge) ─────────


def _threshold_cache_key(
    market: str, symbol: str, kind: str, lookback: int, day: date,
) -> str:
    return f"alert:thr:{market}:{symbol}:{kind}:{lookback}:{day.isoformat()}"


async def _resolve_thresholds(
    db: AsyncSession,
    market: str,
    symbol: str,
    needs: set[tuple[str, int]],
) -> dict[tuple[str, int], float | None]:
    """Resolve every (kind, lookback) threshold the alert batch needs.

    Redis-cached per symbol per day (key embeds today's date) so the
    per-tick cost after the first resolution is one cache read — not
    an ohlcv_daily aggregate per tick. A None value ("not enough daily
    bars") is cached too, wrapped in {"value": None}, so symbols
    without history don't re-query the DB 390 times a session.
    """
    out: dict[tuple[str, int], float | None] = {}
    if not needs:
        return out
    today = date.today()
    for kind, lookback in needs:
        key = _threshold_cache_key(market, symbol, kind, lookback, today)
        cached = await cache_get_json(key)
        if isinstance(cached, dict) and "value" in cached:
            out[(kind, lookback)] = cached["value"]
            continue
        value = await _compute_threshold(db, market, symbol, kind, lookback, today)
        out[(kind, lookback)] = value
        await cache_set_json(key, {"value": value}, TTL_ALERT_THRESHOLD)
    return out


async def _compute_threshold(
    db: AsyncSession,
    market: str,
    symbol: str,
    kind: str,
    lookback: int,
    today: date,
) -> float | None:
    """N-day high / low / avg volume over the last `lookback` daily
    bars strictly before today (today's live bar must not count as
    its own breakout reference)."""
    stmt = (
        select(OhlcvDaily)
        .where(
            OhlcvDaily.market == market,
            OhlcvDaily.symbol == symbol,
            OhlcvDaily.ts < today,
        )
        .order_by(OhlcvDaily.ts.desc())
        .limit(lookback)
    )
    rows = (await db.scalars(stmt)).all()
    if kind == THR_HIGH:
        vals = [float(r.high) for r in rows if r.high is not None]
        return max(vals) if vals else None
    if kind == THR_LOW:
        vals = [float(r.low) for r in rows if r.low is not None]
        return min(vals) if vals else None
    if kind == THR_AVG_VOL:
        vols = [float(r.volume) for r in rows if r.volume is not None]
        return (sum(vols) / len(vols)) if vols else None
    return None
