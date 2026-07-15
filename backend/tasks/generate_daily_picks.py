import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from db.session import AsyncSessionLocal
from models.stock_report import StockReport
from models.user import User
from services.daily_pick_service import NoEligibleCandidatesError, generate_daily_picks

log = logging.getLogger(__name__)


async def run_market(market: str) -> None:
    try:
        local_now = datetime.now(
            ZoneInfo("Asia/Taipei" if market == "TW" else "America/New_York"),
        )
        if local_now.weekday() >= 5:
            log.info("generate_daily_picks: market=%s skipped=weekend", market)
            return
        async with AsyncSessionLocal() as db:
            user_ids = set((await db.scalars(
                select(StockReport.user_id)
                .join(User, User.id == StockReport.user_id)
                .where(
                    StockReport.market == market,
                    StockReport.created_at >= datetime.now(UTC) - timedelta(days=7),
                    User.is_active.is_(True),
                )
                .distinct()
            )).all())
            generated = 0
            for user_id in user_ids:
                try:
                    await generate_daily_picks(db, user_id, market)
                    generated += 1
                except NoEligibleCandidatesError:
                    continue
            log.info("generate_daily_picks: market=%s generated=%d", market, generated)
    except Exception:
        log.exception("generate_daily_picks failed for market=%s", market)


async def run_tw() -> None:
    await run_market("TW")


async def run_us() -> None:
    await run_market("US")
