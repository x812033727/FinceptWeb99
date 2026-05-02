"""Self-grade discussion conclusions — runs daily after TW close.

Cron: 08:30 UTC = 16:30 Asia/Taipei (3h after TW close at 13:30 Taipei).
Picks up every discussion (manual + auto-run, PR #218) whose
`verify_after_date <= today` and `verdict IS NULL`, fetches OHLCV
bars for the recommended symbols over the 5-trading-day window
starting from the discussion's day-1, and computes win/loss:

  win  = max(high) >= day1_open × 1.03 for ANY recommended symbol
  loss = no symbol crossed the threshold and at least one symbol's
         5-day window resolved against `tw_market_service.get_history`
  unverifiable = synthesizer returned no symbols (set immediately;
                 nothing to grade)

Defer (verdict stays NULL, retry tomorrow) when none of the symbols
have 5 bars yet — that's a holiday-in-window or a delisting issue;
the cron's daily cadence + idempotency make a re-attempt safe.

Notes on day-1 open snapshot:
  We don't capture day1_open at auto-run time (00:00 UTC, market
  hasn't opened). The verifier captures it lazily from the first
  history bar at or after the discussion's TW-local creation date,
  caches it on the discussion row, and uses it for the gain calc.
  Storing the snapshot pins the comparison against the original
  open price even if the upstream connector later corrects the bar.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime

from sqlalchemy import select, update

from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from models.discussion import Discussion
from services import tw_market_service
from services.ingest.repository import (
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    record_failure,
    record_health,
)
from services.tw_trading_calendar import to_tw_date

log = logging.getLogger(__name__)

JOB_ID = "verify_discussion_outcome"
_LOCK_KEY = "lock:verify_discussion_outcome"
_LOCK_TTL = 10 * 60

_WIN_THRESHOLD = 0.03
_WINDOW_TRADING_DAYS = 5
_TW_SYMBOL_RE = re.compile(r"^\d{4,6}$")


async def run() -> None:
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("verify_discussion_outcome.skipped_lock_held")
        return
    try:
        remaining = await backoff_remaining_seconds(JOB_ID)
        if remaining > 0:
            failures = await get_failure_count(JOB_ID)
            mins = max(1, remaining // 60)
            log.info(
                "verify_discussion_outcome.skipped_backoff",
                extra={"failures": failures, "seconds_remaining": remaining},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=(
                    f"skipped (backoff after {failures} failures, "
                    f"~{mins} min remaining)"
                ),
            )
            return

        try:
            verified_count = await _do_run()
        except Exception as exc:
            failures = await record_failure(JOB_ID)
            log.warning(
                "verify_discussion_outcome.failed",
                extra={"error": str(exc), "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"{exc} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info(
            "verify_discussion_outcome.done",
            extra={"rows_processed": verified_count},
        )
        await record_health(JOB_ID, ok=True, row_count=verified_count)
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> int:
    async with AsyncSessionLocal() as db:
        today = datetime.now(UTC).date()
        # `auto_run` filter dropped (PR #218): manual discussions
        # had `verify_after_date` set by `synthesize_conclusion` since
        # the same PR, so the same date-based gate now covers both
        # paths. Verdict on manual rows is what powers the
        # `prior_discussions.verdict` field — without it, every
        # cross-session reference surfaced `verdict: null`, making
        # the consistency-check feature mostly noise.
        pending = (await db.scalars(
            select(Discussion).where(
                Discussion.verdict.is_(None),
                Discussion.verify_after_date.is_not(None),
                Discussion.verify_after_date <= today,
            )
        )).all()
        log.info(
            "verify_discussion_outcome.pending",
            extra={"count": len(pending), "today": today.isoformat()},
        )

        verified = 0
        for d in pending:
            try:
                wrote = await _verify_one(db, d)
                if wrote:
                    verified += 1
            except Exception:
                # Per-row failure shouldn't poison the whole batch;
                # next cycle retries. Log with full traceback.
                log.exception(
                    "verify_discussion_outcome.row_failed",
                    extra={"id": str(d.id)},
                )
                continue
        return verified


async def _verify_one(db, d: Discussion) -> bool:
    """Compute and persist the verdict for one row. Returns True if a
    verdict was written, False if we deferred (waiting for more bars).

    Uses the atomic-UPDATE + manual-mirror pattern (PR #114) to avoid
    SQLAlchemy's "Instance not persistent" error on PostgreSQL when
    `synchronize_session='auto'` expunges the entity after an ORM
    UPDATE."""
    raw_symbols = (d.conclusion or {}).get("recommended_symbols") or []
    symbols = [
        str(s).strip()
        for s in raw_symbols
        if _TW_SYMBOL_RE.fullmatch(str(s).strip())
    ]

    # Edge: no symbols recommended → unverifiable. Set immediately so
    # the cron doesn't keep re-fetching this row forever.
    if not symbols:
        await _set_verdict(
            db, d,
            verdict="unverifiable",
            reason="synthesizer returned no symbols",
            day1_opens={},
        )
        return True

    day1_opens: dict[str, float] = dict(d.day1_open_prices or {})
    day5_closes: dict[str, float] = dict(d.day5_close_prices or {})
    created_tw = to_tw_date(d.created_at)

    win_symbol: str | None = None
    win_gain: float | None = None
    best_gain: float = float("-inf")
    best_symbol: str | None = None
    resolved_any = False

    for sym in symbols:
        bars = await tw_market_service.get_history(sym, months=1)
        # Bars are returned with `time` either as ISO date ("YYYY-MM-DD")
        # or full ISO timestamp. Truncate to date for comparison.
        future_bars = sorted(
            (b for b in bars if _bar_date(b) >= created_tw),
            key=lambda b: b.get("time", ""),
        )
        window = future_bars[:_WINDOW_TRADING_DAYS]
        # Need the full 5-bar window before grading — a holiday inside
        # the 5-day span means the 5th trading-day bar hasn't landed
        # yet. The cron's daily cadence retries safely once it does.
        if len(window) < _WINDOW_TRADING_DAYS:
            continue

        if sym not in day1_opens:
            day1_open = _coerce_float(window[0].get("open"))
            if day1_open is None or day1_open <= 0:
                continue
            day1_opens[sym] = day1_open
        day1_open = day1_opens[sym]
        if day1_open <= 0:
            continue

        # Capture the day-5 close once (lazy snapshot, same pattern as
        # day1_open). Used by the frontend to render
        # `4958:55/51 (-7.3%)` per symbol; pinning it here means the
        # display value doesn't drift if the upstream connector later
        # corrects the bar.
        if sym not in day5_closes:
            day5_close = _coerce_float(window[-1].get("close"))
            if day5_close is not None and day5_close > 0:
                day5_closes[sym] = day5_close

        highs = [
            h for h in (_coerce_float(b.get("high")) for b in window)
            if h is not None
        ]
        if not highs:
            continue

        max_high = max(highs)
        gain = (max_high - day1_open) / day1_open
        resolved_any = True
        if gain >= _WIN_THRESHOLD:
            win_symbol = sym
            win_gain = gain
            break
        if gain > best_gain:
            best_gain = gain
            best_symbol = sym

    if win_symbol is not None:
        await _set_verdict(
            db, d,
            verdict="win",
            reason=f"{win_symbol} max high {(win_gain or 0) * 100:.1f}% over day1 open",
            day1_opens=day1_opens,
            day5_closes=day5_closes,
        )
        return True

    # No winner found. Two sub-cases: (a) at least one symbol resolved
    # but came up short → loss, (b) nothing resolved → defer.
    if resolved_any:
        reason = (
            f"best gain {best_gain * 100:.1f}% by {best_symbol} "
            f"(under {_WIN_THRESHOLD * 100:.0f}% threshold)"
            if best_symbol is not None
            else f"no symbol crossed {_WIN_THRESHOLD * 100:.0f}% threshold"
        )
        await _set_verdict(
            db, d,
            verdict="loss",
            reason=reason,
            day1_opens=day1_opens,
            day5_closes=day5_closes,
        )
        return True

    log.info(
        "verify_discussion_outcome.deferred_no_data",
        extra={"id": str(d.id), "symbols": symbols},
    )
    return False


async def _set_verdict(
    db,
    d: Discussion,
    *,
    verdict: str,
    reason: str,
    day1_opens: dict[str, float],
    day5_closes: dict[str, float] | None = None,
) -> None:
    now = datetime.now(UTC)
    closes = day5_closes if day5_closes is not None else {}
    await db.execute(
        update(Discussion)
        .where(Discussion.id == d.id)
        .values(
            verdict=verdict,
            verdict_reason=reason,
            verified_at=now,
            day1_open_prices=day1_opens or None,
            day5_close_prices=closes or None,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    # Mirror onto the in-memory entity so any callers that hold the
    # row reference see the new state (PR #114 pattern).
    d.verdict = verdict
    d.verdict_reason = reason
    d.verified_at = now
    d.day1_open_prices = day1_opens or None
    d.day5_close_prices = closes or None
    log.info(
        "verify_discussion_outcome.verdict",
        extra={
            "id": str(d.id),
            "verdict": verdict,
            "reason": reason,
        },
    )


def _bar_date(bar: dict) -> date:
    raw = str(bar.get("time", "") or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return date.min


def _coerce_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
