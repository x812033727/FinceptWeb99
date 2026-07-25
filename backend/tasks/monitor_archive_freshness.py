"""Content-level freshness check on the TW archive tables.

Job-level ingest health has proven unreliable in both directions
(`failed` on a 15k-row run, `ok` on zero-row runs), so this job asks
the only witness that cannot lie: the data. For each dataset table it
reads `max(ts)` and compares against the expected settled session.

Scheduled 19:00 UTC = 03:00 Taipei — after the evening ingest window
(all TW ingest lands by 19:30 Taipei) and one hour before the 04:00
daily discussion, so a gap is recorded before the discussion would hit
its archive-first live fallback.

`record_health`'s `latest_data_ts` is stamped with the OLDEST date
actually observed across the four datasets (`min` of what `_collect_
latest` found), not the expected/desired session. Stamping the
desired session would show a fresh-looking timestamp on the admin
dashboard right beside `ok=False` — the honest "data is at least this
fresh" bound is the weakest dataset actually on hand, not the date we
wished for.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import AsyncSessionLocal
from models.ohlcv_daily import OhlcvDaily
from models.tw_chip_metrics import TwInstitutionalDaily, TwMarginDaily
from services.ingest.repository import record_health
from services.tw_trading_calendar import prev_trading_day_estimate

log = logging.getLogger(__name__)

JOB_ID = "monitor_archive_freshness"


def stale_datasets(
    latest: dict[str, date | None], expected: date,
) -> list[str]:
    """Datasets whose newest row predates the expected session, as
    human-readable findings. Pure so the policy is table-testable."""
    findings: list[str] = []
    for key, newest in latest.items():
        if newest is None:
            findings.append(f"{key}: empty (expected {expected})")
        elif newest < expected:
            findings.append(f"{key}: {newest} (expected {expected})")
    return findings


async def _collect_latest(db: AsyncSession | None = None) -> dict[str, date | None]:
    """`db` is an optional seam for tests: pass a session to skip
    `AsyncSessionLocal` entirely instead of relying on it being
    monkeypatched. Production callers always omit it."""
    if db is not None:
        return await _collect_latest_with(db)
    async with AsyncSessionLocal() as session:
        return await _collect_latest_with(session)


async def _collect_latest_with(db: AsyncSession) -> dict[str, date | None]:
    # All three ts columns are `Date`, never `DateTime`, so `func.max`
    # always returns a plain `date` — no `datetime`-with-`.date()` case
    # to unwrap.
    def _as_date(v):
        return v

    ohlcv = await db.scalar(
        select(func.max(OhlcvDaily.ts)).where(
            OhlcvDaily.market == "TW",
            # `escape="\\"` is load-bearing, not decorative: `_` is
            # the LIKE single-char wildcard. The former
            # `~symbol.startswith("_")` compiled to
            # `NOT LIKE '_' || '%'` with no ESCAPE clause, which
            # matches every non-empty symbol (so NOT LIKE excluded
            # everything, and `ohlcv_tw` was always None). Without an
            # explicit ESCAPE clause `\_%` still doesn't filter on
            # SQLite (verified: backslash is a literal there, not an
            # escape char by default) — the clause must stay explicit
            # for both backends.
            OhlcvDaily.symbol.not_like(r"\_%", escape="\\"),
        )
    )
    taiex = await db.scalar(
        select(func.max(OhlcvDaily.ts)).where(
            OhlcvDaily.market == "TW",
            OhlcvDaily.symbol == "_TAIEX",
        )
    )
    inst = await db.scalar(select(func.max(TwInstitutionalDaily.ts)))
    margin = await db.scalar(select(func.max(TwMarginDaily.ts)))
    return {
        "ohlcv_tw": _as_date(ohlcv) if ohlcv else None,
        "taiex": _as_date(taiex) if taiex else None,
        "institutional_tw": _as_date(inst) if inst else None,
        "margin_tw": _as_date(margin) if margin else None,
    }


async def run() -> None:
    now_tw = datetime.now(ZoneInfo("Asia/Taipei"))
    # At 03:00 Taipei the expected newest session is the previous
    # trading day — the one the evening ingest wrote.
    expected = prev_trading_day_estimate(now_tw.date())
    latest = await _collect_latest()
    findings = stale_datasets(latest, expected)
    # Number of datasets found fresh, not a row count in the usual
    # ingest-task sense — this job never writes rows, it only reads.
    fresh = len(latest) - len(findings)
    if findings:
        log.warning(
            "monitor_archive_freshness.stale",
            extra={"expected": expected.isoformat(), "findings": findings},
        )
    # Stamp what we actually found (oldest observed dataset date), not
    # `expected` — `expected` is the session we wanted, and stamping
    # it would show a fresh-looking latest_data_ts beside ok=False.
    observed = [d for d in latest.values() if d is not None]
    await record_health(
        JOB_ID,
        ok=not findings,
        row_count=fresh,
        error="; ".join(findings) if findings else None,
        latest_data_ts=min(observed) if observed else None,
    )
