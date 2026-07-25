"""Outcome taxonomy for the chip-ledger ingest walks (spec Track 1).

`ok 0 rows` used to mean any of: nothing due, source not published yet,
or source silently broken. The discriminator between the last two is the
requested day's age plus a trading-day witness — our own price archive.
A past day with price bars but no chip rows is a `gap` (not-ok, the
silent-failure class); today with no rows is `not_yet_published`
(expected before the evening publication; the 21:40 re-probe run exists
for exactly this).
"""
from __future__ import annotations

from datetime import date


def classify_chip_outcome(
    *,
    day_rows: dict[date, int],
    today: date,
    traded: set[date],
) -> tuple[bool, str | None]:
    if not day_rows:
        return True, "idle: nothing due"
    gaps = sorted(
        d for d, rows in day_rows.items()
        if rows == 0 and d < today and d in traded
    )
    if gaps:
        return False, "gap: " + ", ".join(d.isoformat() for d in gaps)
    if day_rows.get(today, None) == 0:
        return True, f"not_yet_published: {today.isoformat()}"
    return True, None
