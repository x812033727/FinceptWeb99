"""Weekly episodic→semantic lesson promotion. Wired into the scheduler;
`promote_eligible_lessons` is otherwise only reachable via an admin
endpoint, which is why the semantic tier stayed empty."""
import logging

from db.session import AsyncSessionLocal
from services.lesson_tier_service import promote_eligible_lessons

log = logging.getLogger(__name__)

LESSON_MARKETS = ("TW",)


async def run() -> None:
    async with AsyncSessionLocal() as db:
        for market in LESSON_MARKETS:
            try:
                promoted = await promote_eligible_lessons(db, market=market)
                if promoted:
                    log.info("promote_lessons.promoted",
                             extra={"market": market, "count": len(promoted)})
            except Exception as exc:
                log.warning("promote_lessons.failed",
                            extra={"market": market, "error": str(exc)})
