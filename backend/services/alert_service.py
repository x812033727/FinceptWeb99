"""
Price alert CRUD + check-and-fire logic called from market refresh tasks.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.alert import AlertCondition, PriceAlert
from api.alerts.schemas import AlertCreate


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
    async def check_and_fire(
        db: AsyncSession,
        symbol: str,
        market: str,
        current_price: float,
    ) -> None:
        """
        Check untriggered alerts for this symbol+market, fire any that match.
        Pushes alert notifications to connected WebSocket clients via the WS manager.
        Called from market refresh tasks.
        """
        from api.websocket.manager import push_alert_to_user

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
                alert.triggered = True
                alert.triggered_at = datetime.now(timezone.utc)
                fired.append(alert)

        if fired:
            await db.commit()
            for alert in fired:
                await push_alert_to_user(str(alert.user_id), {
                    "type": "alert",
                    "id": str(alert.id),
                    "symbol": alert.symbol,
                    "market": alert.market,
                    "condition": alert.condition.value,
                    "target_price": alert.target_price,
                    "current_price": current_price,
                })
