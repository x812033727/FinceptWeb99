import logging

from db.session import AsyncSessionLocal
from services.decision_journal_service import refresh_decision_journal

log = logging.getLogger(__name__)


async def run() -> None:
    try:
        async with AsyncSessionLocal() as db:
            changed = await refresh_decision_journal(db)
        log.info("refresh_decision_journal: refreshed %d entries", changed)
    except Exception:
        log.exception("refresh_decision_journal failed")
