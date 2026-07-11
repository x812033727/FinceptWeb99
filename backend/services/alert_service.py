"""
Price alert CRUD + check-and-fire logic called from market refresh tasks.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.alert import AlertCondition, AlertEvent, PriceAlert
from schemas.alert import AlertCreate
from services.notification_service import notify_user


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
        )
        db.add(alert)
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
    ) -> None:
        """
        Check untriggered alerts for this symbol+market, fire any that match.
        Pushes alert notifications via the registered notification transport
        (WebSocket in production). Called from market refresh tasks.
        """
        result = await db.execute(
            select(PriceAlert).where(
                and_(
                    PriceAlert.symbol == symbol,
                    PriceAlert.market == market,
                    PriceAlert.triggered.is_(False),
                )
            )
        )
        alerts = list(result.scalars().all())
        if not alerts:
            return

        fired: list[PriceAlert] = []
        for alert in alerts:
            triggered = (
                alert.condition == AlertCondition.above and current_price >= alert.target_price
            ) or (
                alert.condition == AlertCondition.below and current_price <= alert.target_price
            )
            if triggered:
                now = datetime.now(timezone.utc)
                alert.triggered = True
                alert.triggered_at = now
                # History row lands in the SAME transaction as the flag
                # flip — either both persist or neither (PR-D5).
                direction = "高於" if alert.condition == AlertCondition.above else "低於"
                db.add(AlertEvent(
                    user_id=alert.user_id,
                    alert_id=alert.id,
                    symbol=alert.symbol,
                    market=alert.market,
                    kind="price",
                    message=(
                        f"{alert.symbol} 價格{direction}目標 "
                        f"{alert.target_price:g}(現價 {current_price:g})"
                    ),
                    fired_at=now,
                    payload={
                        "condition": alert.condition.value,
                        "target_price": alert.target_price,
                        "current_price": current_price,
                    },
                ))
                fired.append(alert)

        if fired:
            await db.commit()
            for alert in fired:
                await notify_user(str(alert.user_id), {
                    "type": "alert",
                    "id": str(alert.id),
                    "symbol": alert.symbol,
                    "market": alert.market,
                    "condition": alert.condition.value,
                    "target_price": alert.target_price,
                    "current_price": current_price,
                })
