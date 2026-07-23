"""Replay the daily strategy discussions over past sessions.

Three discussions a day, most of them still inside their grading
window, is not enough decided rounds to say anything about whether the
panel is any good. This walks backwards over trading days, running the
same auto-run pipeline with `as_of` set, so each session gets the
candidate pool and the market context that were actually knowable then.

Look-ahead is prevented in three independent places, none of them added
here:

  - `load_candidate_rows(as_of=...)` clamps every screener source to
    `ts <= as_of`
  - the context builder anchors every block to
    `prev_trading_day(as_of)` (`info_cutoff`), so the panel never sees
    the session it is predicting
  - `verify_discussion_outcome` grades a row with `as_of_date` set from
    the OHLCV archive forward of that date, not from a live quote

`--verify-only` re-checks the first of those against what actually
landed in `discussion_round_contexts`, which is the assertion worth
making before spending real money on a long run.

Cost: each discussion is ~480k prompt tokens (five rounds, eight
personas) — about US$1.6 at the current auto-run model. A 60-session
three-strategy run is therefore ~180 discussions and ~US$285, so the
budget ceiling is a hard stop rather than a warning, and progress is
per-session so an interrupted run resumes where it left off.

Usage::

    # What would run, and what it would cost — no LLM calls
    python -m scripts.replay_daily_discussions --sessions 60 --dry-run

    # Smoke test: three sessions, then check for look-ahead
    python -m scripts.replay_daily_discussions --sessions 3
    python -m scripts.replay_daily_discussions --sessions 3 --verify-only

    # Full run under a spend ceiling
    python -m scripts.replay_daily_discussions --sessions 60 --budget-usd 300

    # Named sessions instead of a window — for stratified sampling
    python -m scripts.replay_daily_discussions \
        --dates 2026-04-29,2026-05-22,2026-06-16 --budget-usd 40

A contiguous window is a convenience, not a sampling method. Whether
the panel finds a trade depends heavily on the regime — measured over
the first replays, a session at VIX 33.9 produced picks from two of
three strategies while sessions at 35.2-42.3 produced none — so a
window that happens to sit inside one volatility band answers only
for that band. When the goal is measuring accuracy rather than
covering recent history, draw `--dates` evenly across bands.

Run it from the host, not from inside the backend container: a deploy
recreates the container and would take a long-running replay with it.
Already-replayed slots are skipped (the auto-run unique constraint
covers `(owner, date, strategy, sequence)`), so re-running is safe.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, timedelta

from sqlalchemy import func, select

from db.session import AsyncSessionLocal
from models.discussion import Discussion
from models.llm_usage_event import LLMUsageEvent
from models.ohlcv_daily import OhlcvDaily
from services import discussion_auto_run_config_service
from tasks.auto_run_discussion import _run_for_user

log = logging.getLogger(__name__)

# Measured over the six live sessions to 2026-07-22.
_USD_PER_DISCUSSION = 1.58
_BENCHMARK_SYMBOL = "_TAIEX_TR"


async def _archived_sessions_among(wanted: list[date]) -> list[date]:
    """Those of `wanted` that are archived trading sessions, oldest first.

    Same source of truth as `_trading_sessions` — the index series
    rather than a calendar — so a public holiday named on the command
    line is dropped by observation rather than silently replayed
    against no bars.
    """
    if not wanted:
        return []
    async with AsyncSessionLocal() as db:
        rows = await db.scalars(
            select(OhlcvDaily.ts)
            .where(
                OhlcvDaily.market == "TW",
                OhlcvDaily.symbol == _BENCHMARK_SYMBOL,
                OhlcvDaily.ts.in_(wanted),
            )
            .distinct()
            .order_by(OhlcvDaily.ts)
        )
        return [r if isinstance(r, date) else r.date() for r in rows.all()]


async def _trading_sessions(count: int, end: date) -> list[date]:
    """The `count` most recent archived trading sessions on/before
    `end`, oldest first.

    Taken from the index series rather than a calendar, so holidays are
    excluded by observation instead of by a hardcoded table that goes
    wrong once a year.
    """
    async with AsyncSessionLocal() as db:
        rows = await db.scalars(
            select(OhlcvDaily.ts)
            .where(
                OhlcvDaily.market == "TW",
                OhlcvDaily.symbol == _BENCHMARK_SYMBOL,
                OhlcvDaily.ts <= end,
            )
            .distinct()
            .order_by(OhlcvDaily.ts.desc())
            .limit(count)
        )
        days = [r if isinstance(r, date) else r.date() for r in rows.all()]
    return sorted(days)


async def _spent_usd() -> float:
    async with AsyncSessionLocal() as db:
        return float(await db.scalar(
            select(func.coalesce(func.sum(LLMUsageEvent.cost_usd), 0)),
        ) or 0.0)


async def _covered_sessions(owner_id, days: list[date]) -> set[date]:
    """Sessions that already have an auto-run row, live or replayed.

    Keyed on `auto_run_date`, because that is the column
    `_run_for_user` dedupes against — the unique slot is
    `(owner, auto_run_date, strategy, sequence)`. Keying this on
    `as_of_date` instead (which is NULL on live rows) made the script
    ask for sessions the pipeline would then silently skip, and report
    them as replayed: three sessions "replayed", nine discussions
    promised, nothing created, US$0.00 spent.

    A session with a live run does not want a replay anyway. The live
    row is the better datum — it is what the panel actually produced
    that morning — and a second row for the same session would double-
    count it in every rate the scoreboard computes.
    """
    if not days:
        return set()
    async with AsyncSessionLocal() as db:
        rows = await db.scalars(
            select(Discussion.auto_run_date).where(
                Discussion.owner_id == owner_id,
                Discussion.auto_run_date.in_(days),
                Discussion.auto_run.is_(True),
            ).distinct()
        )
        return {r for r in rows.all() if r is not None}


async def _verify_no_lookahead(owner_id, days: list[date]) -> int:
    """Assert no replayed round context carries a date after its anchor.

    Reads what actually landed rather than trusting the clamp, because
    a replay built on leaked future prices would look great and mean
    nothing. Returns the number of violations found.
    """
    from models.discussion_round_context import DiscussionRoundContext

    violations = 0
    checked = 0
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Discussion.id, Discussion.as_of_date)
            .where(
                Discussion.owner_id == owner_id,
                Discussion.as_of_date.in_(days),
                Discussion.auto_run.is_(True),
            )
        )).all()
        for discussion_id, as_of in rows:
            contexts = (await db.scalars(
                select(DiscussionRoundContext.context)
                .where(DiscussionRoundContext.discussion_id == discussion_id)
            )).all()
            for ctx in contexts:
                checked += 1
                for where, bad in _future_dates(ctx, as_of):
                    violations += 1
                    print(f"  LOOK-AHEAD {discussion_id} as_of={as_of} "
                          f"{where} -> {bad}")
    print(f"checked {checked} round contexts across {len(rows)} discussions")
    return violations


def _future_dates(node, as_of: date, path: str = ""):
    """Yield `(path, value)` for every `as_of`-ish date field later than
    the anchor. Only date-typed fields named like a session stamp are
    considered; free text is left alone."""
    stamp_keys = ("as_of", "as_of_session", "session_date", "latest_date",
                  "from_ts", "time", "ts", "date")
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if key in stamp_keys and isinstance(value, str):
                try:
                    parsed = date.fromisoformat(value[:10])
                except ValueError:
                    continue
                if parsed > as_of:
                    yield child, value
                continue
            yield from _future_dates(value, as_of, child)
    elif isinstance(node, list):
        for index, value in enumerate(node[:50]):
            yield from _future_dates(value, as_of, f"{path}[{index}]")


async def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sessions", type=int, default=60)
    ap.add_argument("--end", type=date.fromisoformat, default=None,
                    help="latest session to replay (default: yesterday)")
    ap.add_argument("--dates", default=None,
                    help="comma-separated sessions to replay instead of a "
                         "contiguous window — for stratified sampling "
                         "(e.g. equal counts across volatility bands)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-only", action="store_true",
                    help="check replayed contexts for look-ahead, run nothing")
    ap.add_argument("--budget-usd", type=float, default=None,
                    help="stop before exceeding this much additional spend")
    args = ap.parse_args(argv)

    # Never replay a session whose own 5-day window is still open enough
    # to be ungradeable, and never replay today (its close isn't in yet).
    end = args.end or (date.today() - timedelta(days=1))
    if args.dates:
        try:
            wanted = sorted({
                date.fromisoformat(token.strip())
                for token in args.dates.split(",")
                if token.strip()
            })
        except ValueError as exc:
            print(f"--dates: {exc}", file=sys.stderr)
            return 2
        days = await _archived_sessions_among(wanted)
        missing = sorted(set(wanted) - set(days))
        if missing:
            # Named but unarchived days are reported, never silently
            # dropped: a stratified sample that quietly loses a band
            # is worse than one that fails loudly.
            print(
                "not archived trading sessions, skipping: "
                + ", ".join(d.isoformat() for d in missing),
                file=sys.stderr,
            )
    else:
        days = await _trading_sessions(args.sessions, end)
    if not days:
        print("no archived trading sessions found", file=sys.stderr)
        return 1

    async with AsyncSessionLocal() as db:
        configs = await discussion_auto_run_config_service.list_enabled(db)
    if not configs:
        print("no enabled auto-run config", file=sys.stderr)
        return 1
    cfg = configs[0]

    if args.verify_only:
        violations = await _verify_no_lookahead(cfg.user_id, days)
        print(f"look-ahead violations: {violations}")
        return 1 if violations else 0

    done = await _covered_sessions(cfg.user_id, days)
    todo = [d for d in days if d not in done]
    strategies = sum(
        1 for count in discussion_auto_run_config_service
        .normalize_strategy_run_counts(
            getattr(cfg, "strategy_run_counts", None), legacy_enabled=True,
        ).values() if count > 0
    )
    estimate = len(todo) * strategies * _USD_PER_DISCUSSION
    print(f"sessions {days[0]} .. {days[-1]}  "
          f"({len(days)} total, {len(done)} already covered, {len(todo)} to run)")
    if not todo:
        print("nothing to replay — every session in the window already has "
              "an auto-run row")
        return 0
    print(f"~{len(todo) * strategies} discussions, ~US${estimate:.0f}")
    if args.dry_run:
        return 0

    baseline = await _spent_usd()
    ran = 0
    empty = 0
    for day in todo:
        if args.budget_usd is not None:
            spent = await _spent_usd() - baseline
            if spent + strategies * _USD_PER_DISCUSSION > args.budget_usd:
                print(f"stopping before {day}: spent US${spent:.2f} of "
                      f"US${args.budget_usd:.2f} ceiling")
                break
        async with AsyncSessionLocal() as db:
            fresh = (await discussion_auto_run_config_service.list_enabled(db))[0]
            # `_run_for_user` returns False when it created nothing —
            # every slot taken, or every strategy's screener came back
            # empty for that session. Report that instead of counting it
            # as a replay; a run that produces no rows while printing
            # progress is the exact failure this script just had.
            created = await _run_for_user(db, fresh, as_of=day)
        if created:
            ran += 1
            print(f"{day}  replayed ({ran}/{len(todo)})")
        else:
            empty += 1
            print(f"{day}  produced nothing (no candidates, or slots taken)")

    spent = await _spent_usd() - baseline
    print(f"replayed {ran} sessions ({empty} produced nothing), "
          f"spent US${spent:.2f}")
    # Asking for work and getting none back is a failure, not a quiet
    # success — the caller is about to spend hundreds of dollars on the
    # strength of a smoke test.
    return 1 if ran == 0 else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
