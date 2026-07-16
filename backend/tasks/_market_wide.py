"""Walking FinMind's single-date market-wide datasets.

FinMind's market-wide mode (`data_id=""`) answers with the rows dated
exactly `start_date` for several datasets and silently ignores
`end_date`. It is a one-day snapshot, not a range — despite a signature
(and upstream docs) that read like a range. Verified against the live
API, both with `data_id=""` and with the parameter omitted entirely:

    TaiwanStockDayTrading  start=2026-07-01 end=2026-07-15 -> 1 date
    TaiwanStockShareholding                     "          -> 1 date
    TaiwanStockGovernmentBankBuySell            "          -> HTTP 400
    TaiwanFuturesInstitutionalInvestors         "          -> 1 date

Not every dataset behaves this way — `TaiwanStockDispositionSecurities
Period` and `TaiwanStockSuspended` do honour ranges, and per-symbol
queries always do. Check before assuming; this helper is for the ones
that don't.

Passing `today - lookback` as the start therefore fetches exactly one
day: the one at the far end of the window. Jobs that did so archived a
single stale session per run and reported ok, leaving their tables
frozen at precisely `today - lookback`.

So a job that wants a window has to ask for each day in it. Weekends
carry no rows, and a day already archived doesn't need re-asking — so
the steady state is one call for today, and an outage heals itself by
picking the gaps back up.
"""
from collections.abc import Awaitable, Callable, Collection
from datetime import date, timedelta
from typing import Any


def pending_market_days(
    today: date,
    lookback_days: int,
    already_have: Collection[date] = (),
) -> list[date]:
    """Weekdays in `[today - lookback_days, today]` not already archived,
    oldest first."""
    have = set(already_have)
    days: list[date] = []
    day = today - timedelta(days=lookback_days)
    while day <= today:
        if day.weekday() < 5 and day not in have:
            days.append(day)
        day += timedelta(days=1)
    return days


async def collect_market_wide(
    fetch: Callable[[str], Awaitable[list[dict[str, Any]]]],
    days: Collection[date],
) -> list[dict[str, Any]]:
    """Concatenate one market-wide fetch per day.

    Errors propagate: a job that half-fetched and reported success is
    the failure mode this whole module exists to undo.
    """
    rows: list[dict[str, Any]] = []
    for day in sorted(days):
        rows.extend(await fetch(day.isoformat()))
    return rows
