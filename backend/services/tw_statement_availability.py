"""Conservative point-in-time availability for Taiwan financial statements.

The FinMind/MOPS statement datasets identify accounting periods by their
period-end dates, not their original publication timestamps. Historical
research must never treat a quarter-end as the date investors could read the
filing. Under Taiwan Securities and Exchange Act Article 36, listed issuers
normally file annual reports within three months and Q1-Q3 reports within 45
days. We add one calendar day and therefore use the filing deadline, not an
optimistic estimated publication date.

This is intentionally conservative but still approximate: early filings are
delayed, while exceptional extensions or late filings are not represented.
Callers must disclose that limitation.
"""
from __future__ import annotations

from datetime import date, timedelta


def statement_available_on(period_end: date) -> date:
    """Return the first date a period is conservatively usable in research."""
    if period_end.month == 12:
        # Annual report deadline: three months after a calendar year end.
        deadline = date(period_end.year + 1, 3, 31)
    else:
        deadline = period_end + timedelta(days=45)
    # Statutory due dates that fall on weekends roll to the next business day.
    # We cannot reconstruct historical public holidays from the statement feed,
    # so that residual uncertainty remains explicitly disclosed by callers.
    while deadline.weekday() >= 5:
        deadline += timedelta(days=1)
    # Waiting one extra day avoids assuming a filing published at the deadline
    # was known at an earlier same-day ranking timestamp.
    return deadline + timedelta(days=1)


def statement_row_available_as_of(row: dict, as_of: date) -> bool:
    try:
        period_end = date.fromisoformat(str(row.get("date", ""))[:10])
    except (TypeError, ValueError):
        return False
    return statement_available_on(period_end) <= as_of
