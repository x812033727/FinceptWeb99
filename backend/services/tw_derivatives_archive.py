"""Reader over the `finmind` archive schema for TAIFEX TX (台指期)
large-trader open-interest concentration and dealer trading breadth —
the first silo of the large-trader feed.

Archive-only by design: unlike `derivatives_service.py` (which calls
the live FinMind connector behind a cache TTL), this module ONLY
reads rows that have already landed in `finmind.*` via the backfill
pipeline. There is no live fallback. When the archive has nothing for
a given cut of contract/date, the whole function returns `None`
rather than fabricating or silently substituting a stale-looking
value — callers must handle the gap explicitly.

Every sub-block carries its own `as_of_session`, taken from the
row's own `ts` column — never from `as_of` (the caller's requested
cutoff) or from "today". The OI concentration block and the dealer
breadth block are populated by independent ingestion jobs on
independent schedules, so they can legitimately disagree about which
session is "latest" (verified 2026-07-25: OI archive newest ts was
stuck on an older session while dealer volume had already advanced
to a later one). Surfacing each block's own date is what makes that
staleness visible to a persona instead of hiding it behind a single
top-level date.

All raw SQL below is schema-qualified as `finmind.<table>` rather
than relying on the connection's `search_path`. This schema hosts
FinMind's raw archive tables at their canonical names, and this repo
already has a precedent for near-duplicate table names living side
by side in `public` (e.g. `ohlcv` / `ohlcv_daily`,
`fundamental_snapshots` / `fundamentals_snapshots`) — an unqualified
query resolving against `search_path` ("$user", public) is one typo
or one future migration away from silently reading the wrong table.
Qualifying every query removes that failure class entirely rather
than relying on no collision existing today.

`as_of` clamps every query to `ts <= as_of` — but only when it is not
`None`. The clause is appended in Python (rather than expressed as
`(:cutoff IS NULL OR ts <= :cutoff)`) so the `:cutoff` bind param is
only ever sent when it has a concrete value; a `date`-vs-NULL bind
with no other type context is a real `asyncpg` footgun (Postgres
can't always infer the parameter's type from `... IS NULL`).
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import text

_CONTRACT = "TX"
_RANKS = ("top5", "top10")


def _latest_oi_sql(has_cutoff: bool):
    """Latest session (<= cutoff, if given) for both large-trader
    concentration ranks. Two-step (subquery for max(ts), then filter
    on it) rather than a window function so the shape stays a plain
    row-set matching what a fake test session can hand back."""
    clause = "AND ts <= :cutoff" if has_cutoff else ""
    return text(
        f"""
        SELECT ts, rank, long_oi, short_oi, long_oi_special, short_oi_special
          FROM finmind.tw_futures_oi_largetraders
         WHERE contract = :contract
           {clause}
           AND ts = (
                 SELECT max(ts)
                   FROM finmind.tw_futures_oi_largetraders
                  WHERE contract = :contract
                    {clause}
               )
        """
    )


# The session 5 trading rows before the latest one (5th distinct ts
# strictly below it) — used for the 5-session net change. No cutoff
# needed here: `latest_ts` is already <= the caller's cutoff.
_PRIOR_OI_SQL = text(
    """
    SELECT ts, rank, long_oi, short_oi, long_oi_special, short_oi_special
      FROM finmind.tw_futures_oi_largetraders
     WHERE contract = :contract
       AND ts = (
             SELECT DISTINCT ts
               FROM finmind.tw_futures_oi_largetraders
              WHERE contract = :contract
                AND ts < :latest_ts
              ORDER BY ts DESC
              OFFSET 4 LIMIT 1
           )
    """
)


def _dealer_sql(has_cutoff: bool):
    """Dealer breadth: `contract = 'total'` rows are each dealer's
    volume summed across all their contracts, so summing again across
    dealer_id for a given ts gives the market-wide dealer total for
    that session. mean20 is the trailing mean over the 20 distinct
    sessions STRICTLY BEFORE the latest one (`d2.ts < latest.ts`) —
    the latest session's own volume must NOT be in its own baseline,
    or comparing it to "20-session mean" is comparing it partly to
    itself (with ~9 archived sessions that's already 1/9 self-weight,
    which systematically damps `vs_20s_mean_pct` toward zero). No
    separate cutoff is needed on the trailing-mean side: `d2.ts <
    latest.ts` and `latest.ts` is already <= the caller's cutoff, so
    every row the mean sees is too. Averages over fewer than 20
    sessions when the archive doesn't have 20 yet."""
    clause = "AND ts <= :cutoff" if has_cutoff else ""
    return text(
        f"""
        SELECT latest.ts, latest.total,
               (SELECT avg(s)
                  FROM (
                        SELECT sum(volume) AS s
                          FROM finmind.tw_futures_dealer_volume d2
                         WHERE d2.contract = 'total'
                           AND d2.ts < latest.ts
                      GROUP BY d2.ts
                      ORDER BY d2.ts DESC
                         LIMIT 20
                       ) t20
               ) AS mean20
          FROM (
                SELECT ts, sum(volume) AS total
                  FROM finmind.tw_futures_dealer_volume
                 WHERE contract = 'total'
                   {clause}
              GROUP BY ts
              ORDER BY ts DESC
                 LIMIT 1
               ) latest
        """
    )


def _oi_block_by_rank(rows: Any) -> dict[str, dict[str, int | None]]:
    """rank -> {long_oi, short_oi, net, special_long, special_short}."""
    blocks: dict[str, dict[str, int | None]] = {}
    for row in rows:
        _ts, rank, long_oi, short_oi, long_oi_special, short_oi_special = row
        net = long_oi - short_oi if long_oi is not None and short_oi is not None else None
        blocks[rank] = {
            "long_oi": long_oi,
            "short_oi": short_oi,
            "net": net,
            "special_long": long_oi_special,
            "special_short": short_oi_special,
        }
    return blocks


async def large_trader_positioning(db: Any, *, as_of: date | None) -> dict[str, Any] | None:
    """Build the large-trader positioning snapshot for TX from the
    archive. Returns `None` when the archive has nothing for
    `contract='TX'` at or before `as_of` (or at all, when `as_of` is
    `None`)."""
    has_cutoff = as_of is not None
    oi_params: dict[str, Any] = {"contract": _CONTRACT}
    if has_cutoff:
        oi_params["cutoff"] = as_of

    latest_rows = (await db.execute(_latest_oi_sql(has_cutoff), oi_params)).all()
    if not latest_rows:
        return None

    latest_ts = latest_rows[0][0]
    latest_by_rank = _oi_block_by_rank(latest_rows)

    prior_rows = (
        await db.execute(_PRIOR_OI_SQL, {"contract": _CONTRACT, "latest_ts": latest_ts})
    ).all()
    prior_by_rank = _oi_block_by_rank(prior_rows) if prior_rows else {}

    dealer_params: dict[str, Any] = {"cutoff": as_of} if has_cutoff else {}
    dealer_rows = (await db.execute(_dealer_sql(has_cutoff), dealer_params)).all()

    result: dict[str, Any] = {"as_of_session": latest_ts.isoformat()}

    net_change_5s: dict[str, int | None] = {}
    for rank in _RANKS:
        block = latest_by_rank.get(rank)
        result[rank] = block
        prior_block = prior_by_rank.get(rank)
        if (
            block is not None
            and block["net"] is not None
            and prior_block is not None
            and prior_block["net"] is not None
        ):
            net_change_5s[rank] = block["net"] - prior_block["net"]
        else:
            net_change_5s[rank] = None
    result["net_change_5s"] = net_change_5s

    if dealer_rows:
        dealer_ts, total, mean20 = dealer_rows[0]
        # sum()/avg() over a `numeric` (bigint) column come back from
        # asyncpg as Decimal — cast to float before doing arithmetic
        # so the result stays JSON-serialisable for the caching layer
        # this feeds (see derivatives_service.py's json.dumps cache).
        vs_20s_mean_pct = None
        if total is not None and mean20:
            vs_20s_mean_pct = round((float(total) - float(mean20)) / float(mean20) * 100, 2)
        result["dealer_volume"] = {
            "as_of_session": dealer_ts.isoformat(),
            "total": int(total) if total is not None else None,
            "vs_20s_mean_pct": vs_20s_mean_pct,
        }
    else:
        result["dealer_volume"] = None

    return result
