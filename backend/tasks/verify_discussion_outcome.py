"""Self-grade discussion conclusions — runs daily after TW close.

Cron: 08:30 UTC = 16:30 Asia/Taipei (3h after TW close at 13:30 Taipei).
Picks up every discussion (manual + auto-run, PR #218) whose
`verify_after_date <= today` and `verdict IS NULL`, fetches OHLCV
bars for the recommended symbols over the 5-trading-day window
starting from the discussion's day-1, and classifies each row into
the 4-band outcome via `services.outcome_classifier.classify_discussion`:

  big_loss  — any symbol's any-day close ≤ big_loss_pct (default -5%)
  big_win   — else any symbol's D5 close ≥ big_win_day5_pct (default 20%)
  win       — else any symbol's D5 close ≥ win_pct (default 5%)
  loss      — otherwise
  unverifiable — synthesizer returned no symbols, OR the stale-grace
                 cap was hit before any bar resolved

All comparisons are on CLOSE prices (matches the post-mortem and
Brier sides), and specifically on the D5 close. Two permissive rules
were retired in turn: first `intra-day high`, which counted single-bar
spikes that round-tripped the same session; then `peak close`, which
still counted a position that rose 6% on D2 and gave it all back by
D5. `peak_pct` / `trough_pct` survive as UI annotations only.
`big_loss` deliberately keeps its any-day-close test — it is a
risk band, not a return band, and a -5% touch is a real drawdown the
holder lived through.

Defer (verdict stays NULL, retry tomorrow) when none of the symbols
have 5 bars yet — that's a holiday-in-window or a delisting issue;
the cron's daily cadence + idempotency make a re-attempt safe.

Notes on day-1 open snapshot:
  We don't capture day1_open at auto-run time (20:00 UTC = 04:00
  Taipei, market still hours from opening). The verifier captures it
  lazily from the first history bar at or after the discussion's
  TW-local creation date, caches it on the discussion row, and uses
  it as the entry-price reference. Storing the snapshot pins the
  comparison against the original open price even if the upstream
  connector later corrects the bar.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from cache.redis_cache import acquire_lock, release_lock
from config import settings
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
from services.outcome_classifier import (
    classify_discussion,
    is_winning_verdict,
)
from services.post_mortem_service import run_post_mortem_pass
from services.tw_trading_calendar import to_tw_date

log = logging.getLogger(__name__)

JOB_ID = "verify_discussion_outcome"
_LOCK_KEY = "lock:verify_discussion_outcome"
_LOCK_TTL = 10 * 60

_WINDOW_TRADING_DAYS = 5
# Stale-grace cap (PR #223): if `today - verify_after_date` exceeds
# this and we still can't resolve a single bar for any recommended
# symbol, give up — verdict='unverifiable'. Without this the cron
# burns OHLCV-fetch quota every day on permanently-stuck rows
# (delisted symbol, malformed code, persistent connector failure).
# 30 days = 6 trading weeks past the would-be resolve date — if the
# data hasn't surfaced by then, it's not coming.
_STALE_GRACE_DAYS = 30
_TW_SYMBOL_RE = re.compile(r"^\d{4,6}$")

DECIDED_VERDICTS = ("win", "big_win", "loss", "big_loss")


def is_experiment(d) -> bool:
    """True for a row produced under a rules text that is not the saved
    production config (`--rules-override` on the replay path).

    Backtest replays SHOULD feed the learning loop — that is what the fuel
    sweeps are for. An experiment row is the one exception: its lesson would
    be "under rules we do not run, this worked", and `promote_lessons` would
    promote it into the semantic lessons that steer the live panel.
    """
    return bool((getattr(d, "candidate_snapshot", None) or {}).get("experiment"))


async def maybe_run_live_post_mortem(db, d) -> None:
    """Self-critique a graded discussion. Only decided outcomes carry
    a real pick worth critiquing; abstain/unverifiable have nothing to
    reflect on. Fail-closed: a post-mortem error must never disturb the
    verdict already committed above."""
    if d.verdict not in DECIDED_VERDICTS or is_experiment(d):
        return
    try:
        await run_post_mortem_pass(db, d, d.owner_id)
    except Exception as exc:
        log.warning("verify_discussion_outcome.live_post_mortem_failed",
                    extra={"id": str(d.id), "error": str(exc)})


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
                JOB_ID,
                ok=False,
                row_count=0,
                error=(f"skipped (backoff after {failures} failures, " f"~{mins} min remaining)"),
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
                JOB_ID,
                ok=False,
                row_count=0,
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


async def _resolve_thresholds(db) -> tuple[float, float, float]:
    """Read the three outcome thresholds (in percent units, e.g. 5.0)
    from runtime config so verifier / post-mortem / Brier stay aligned.
    Falls back to compiled settings on any resolver hiccup so the
    cron never blocks on Redis / runtime-config issues.

    Returns `(big_win_day5_pct, win_pct, big_loss_pct)`.
    """
    big_win = settings.OUTCOME_BIG_WIN_DAY5_THRESHOLD_PCT
    win = settings.OUTCOME_WIN_THRESHOLD_PCT
    big_loss = settings.OUTCOME_BIG_LOSS_THRESHOLD_PCT
    try:
        from services.runtime_config_service import get_float as _get_float

        big_win = await _get_float(db, "OUTCOME_BIG_WIN_DAY5_THRESHOLD_PCT")
        win = await _get_float(db, "OUTCOME_WIN_THRESHOLD_PCT")
        big_loss = await _get_float(db, "OUTCOME_BIG_LOSS_THRESHOLD_PCT")
    except Exception as exc:
        log.debug(
            "verify_discussion_outcome.threshold_fallback",
            extra={"error": str(exc)},
        )
    return big_win, win, big_loss


async def _do_run() -> int:
    async with AsyncSessionLocal() as db:
        today = datetime.now(UTC).date()
        big_win_pct, win_pct, big_loss_pct = await _resolve_thresholds(db)
        # `auto_run` filter dropped (PR #218): manual discussions
        # had `verify_after_date` set by `synthesize_conclusion` since
        # the same PR, so the same date-based gate now covers both
        # paths. Verdict on manual rows is what powers the
        # `prior_discussions.verdict` field — without it, every
        # cross-session reference surfaced `verdict: null`, making
        # the consistency-check feature mostly noise.
        pending = (
            await db.scalars(
                select(Discussion).where(
                    Discussion.verdict.is_(None),
                    Discussion.verify_after_date.is_not(None),
                    # Future-dated rows can't verify yet — bound them out
                    # in SQL instead of pulling and re-filtering in Python.
                    Discussion.verify_after_date <= today,
                )
            )
        ).all()
        log.info(
            "verify_discussion_outcome.pending",
            extra={
                "count": len(pending),
                "today": today.isoformat(),
                "big_win_pct": big_win_pct,
                "win_pct": win_pct,
                "big_loss_pct": big_loss_pct,
            },
        )

        verified = 0
        for d in pending:
            try:
                wrote = await _verify_one(
                    db,
                    d,
                    big_win_pct=big_win_pct,
                    win_pct=win_pct,
                    big_loss_pct=big_loss_pct,
                )
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


async def _verify_one(
    db,
    d: Discussion,
    *,
    big_win_pct: float,
    win_pct: float,
    big_loss_pct: float,
) -> bool:
    """Compute and persist the 4-band verdict for one row. Returns
    True if a verdict was written, False if we deferred (waiting for
    more bars).

    Delegates band classification to
    `services.outcome_classifier.classify_discussion`. Thresholds are
    in PERCENT UNITS (e.g. 5.0 = +5%, -5.0 = -5%) — the classifier
    takes them as-is. All comparisons use CLOSE prices.

    Uses the atomic-UPDATE + manual-mirror pattern (PR #114) to avoid
    SQLAlchemy's "Instance not persistent" error on PostgreSQL when
    `synchronize_session='auto'` expunges the entity after an ORM
    UPDATE."""
    conclusion = d.conclusion or {}
    raw_symbols = conclusion.get("recommended_symbols") or []
    symbols = [str(s).strip() for s in raw_symbols if _TW_SYMBOL_RE.fullmatch(str(s).strip())]

    # Edge: no symbols recommended. Two very different outcomes share
    # this shape and must not share a verdict:
    #
    #   `abstain`      the panel deliberately declined ("no candidate
    #                  meets the entry conditions"). A real, gradeable
    #                  decision — sitting out a −7% tape is a good call,
    #                  and the scoreboard says so by keeping abstains
    #                  out of the win-rate denominator.
    #   `unverifiable` the synthesizer produced nothing usable (parse
    #                  error, empty output). A pipeline failure.
    #
    # The distinction comes from the structured `abstained` flag the
    # synthesizer emits (see `conclusion_parsing._safe_conclusion`),
    # never from `reasoning` prose.
    if not symbols:
        abstained = bool(conclusion.get("abstained"))
        # An abstention still has a counterfactual worth recording:
        # what the deterministic screener pool did that week. Without
        # it we can't tell a wise pass from a missed rally.
        pool_perf: dict[str, Any] | None = None
        if abstained:
            anchor = d.as_of_date or to_tw_date(d.created_at)
            try:
                pool_perf = await _compute_pool_performance(d, anchor)
            except Exception:
                log.exception(
                    "verify_discussion_outcome.pool_performance_failed",
                    extra={"id": str(d.id)},
                )
        await _set_verdict(
            db,
            d,
            verdict="abstain" if abstained else "unverifiable",
            reason=(
                (str(conclusion.get("abstain_reason") or "").strip()
                 or "panel abstained — no candidate met the entry conditions")
                if abstained
                else "synthesizer returned no symbols"
            ),
            day1_opens={},
            pool_performance=pool_perf,
        )
        return True

    day1_opens: dict[str, float] = dict(d.day1_open_prices or {})
    day5_closes: dict[str, float] = dict(d.day5_close_prices or {})
    daily_closes_persist: dict[str, list[float | None]] = dict(
        d.daily_close_prices or {},
    )
    # Backtest mode (PR #224): the post-window starts at `as_of_date`
    # instead of `created_at.date()`. A discussion built today that
    # backtests `as_of='2025-01-15'` is verified against bars from
    # 2025-01-16 onward, not from 2026-05-02.
    if d.as_of_date is not None:
        anchor_tw = d.as_of_date
    else:
        anchor_tw = to_tw_date(d.created_at)

    per_symbol: dict[str, tuple[float, list[float | None]]] = {}

    for sym in symbols:
        if d.as_of_date is not None:
            # Backtest: bars in [as_of, as_of + 14 days] cover the
            # 5-trading-day window plus weekends / holidays. Read
            # the archive directly so we don't pull a live snapshot
            # that would miss historical dates.
            from services.ingest.repository import read_ohlcv_range_autosession

            bars = await read_ohlcv_range_autosession(
                "TW",
                sym,
                d.as_of_date,
                d.as_of_date + timedelta(days=14),
            )
        else:
            bars = await tw_market_service.get_history(sym, months=1)
        # Bars are returned with `time` either as ISO date ("YYYY-MM-DD")
        # or full ISO timestamp. Truncate to date for comparison.
        # `>= anchor_tw` covers backtest (anchor=as_of_date) and
        # live (anchor=created_at.date()) uniformly.
        future_bars = sorted(
            (b for b in bars if _bar_date(b) >= anchor_tw),
            key=lambda b: b.get("time", ""),
        )
        window = future_bars[:_WINDOW_TRADING_DAYS]
        if not window:
            continue

        if sym not in day1_opens:
            day1_open = _coerce_float(window[0].get("open"))
            if day1_open is None or day1_open <= 0:
                continue
            day1_opens[sym] = day1_open
        day1_open = day1_opens[sym]
        if day1_open <= 0:
            continue

        # Pull all 5 closes for the classifier. The last one is
        # also pinned to day5_close_prices for the frontend's
        # `(D5/D1 -7.3%)` rendering.
        observed_closes: list[float | None] = [_coerce_float(b.get("close")) for b in window]
        closes = observed_closes + [None] * (_WINDOW_TRADING_DAYS - len(observed_closes))
        daily_closes_persist[sym] = closes
        if len(window) < _WINDOW_TRADING_DAYS:
            continue
        if sym not in day5_closes:
            last_close = closes[-1]
            if last_close is not None and last_close > 0:
                day5_closes[sym] = last_close
        # Also persist the full D1-D5 close array so the scoreboard /
        # Brier paths can reuse it without re-fetching ohlcv.
        if all(c is None for c in closes):
            continue

        per_symbol[sym] = (day1_open, closes)

    if per_symbol:
        result = classify_discussion(
            per_symbol=per_symbol,
            big_win_day5_pct=big_win_pct,
            win_pct=win_pct,
            big_loss_pct=big_loss_pct,
        )
        if result is not None:
            sym_cls = result.classifications.get(result.winner_symbol or "")
            reason = _format_verdict_reason(
                result.band,
                result.winner_symbol,
                sym_cls,
                big_win_pct=big_win_pct,
                win_pct=win_pct,
                big_loss_pct=big_loss_pct,
            )
            pool_perf: dict[str, Any] | None = None
            try:
                pool_perf = await _compute_pool_performance(d, anchor_tw)
            except Exception:
                # Pool stats are a bonus metric — never block the verdict.
                log.exception(
                    "verify_discussion_outcome.pool_performance_failed",
                    extra={"id": str(d.id)},
                )
            await _set_verdict(
                db,
                d,
                verdict=result.band,
                reason=reason,
                day1_opens=day1_opens,
                day5_closes=day5_closes,
                daily_closes=daily_closes_persist,
                pool_performance=pool_perf,
            )
            return True

    if daily_closes_persist:
        await _set_progress(
            db,
            d,
            day1_opens=day1_opens,
            daily_closes=daily_closes_persist,
        )

    # PR #223 stale-grace: if no symbol resolved AND we're more than
    # `_STALE_GRACE_DAYS` past the original `verify_after_date`, give
    # up and mark unverifiable. Otherwise daily cron retries forever
    # on a permanently-stuck row (delisted code, malformed symbol,
    # persistent connector outage). Day-1 opens may have been
    # captured in a prior tick — pass them through so the verdict
    # reason can hint at what we did manage to see.
    today = datetime.now(UTC).date()
    if d.verify_after_date is not None and ((today - d.verify_after_date).days > _STALE_GRACE_DAYS):
        log.info(
            "verify_discussion_outcome.unverifiable_stale",
            extra={
                "id": str(d.id),
                "symbols": symbols,
                "verify_after": d.verify_after_date.isoformat(),
                "days_overdue": (today - d.verify_after_date).days,
            },
        )
        await _set_verdict(
            db,
            d,
            verdict="unverifiable",
            reason=(
                f"no OHLCV data for any of {symbols} after "
                f"{(today - d.verify_after_date).days} days past "
                f"verify_after_date — likely delisted / malformed symbol"
            ),
            day1_opens=day1_opens,
            day5_closes=day5_closes,
        )
        return True

    log.info(
        "verify_discussion_outcome.deferred_no_data",
        extra={"id": str(d.id), "symbols": symbols},
    )
    return False


async def _compute_pool_performance(
    d: Discussion, anchor_tw: date,
) -> dict[str, Any] | None:
    """Average 5-trading-day close-over-open return of the stored
    sequence-1 candidate pool, read from the local OHLCV archive (no
    live fetches — at verify time the ingest pipeline has these bars).
    None when the row carries no pool; a zero-`resolved` dict when the
    pool exists but no symbol had a complete window yet."""
    snapshot = d.candidate_snapshot if isinstance(d.candidate_snapshot, dict) else {}
    pool = snapshot.get("pool")
    if not isinstance(pool, list) or not pool:
        return None
    symbols = [
        str(item.get("symbol", "")).strip()
        for item in pool
        if isinstance(item, dict)
    ]
    symbols = [s for s in symbols if _TW_SYMBOL_RE.fullmatch(s)]
    if not symbols:
        return None

    from services.ingest.repository import read_ohlcv_range_autosession

    returns: list[float] = []
    for sym in symbols:
        bars = await read_ohlcv_range_autosession(
            "TW", sym, anchor_tw, anchor_tw + timedelta(days=14),
        )
        window = sorted(
            (b for b in bars if _bar_date(b) >= anchor_tw),
            key=lambda b: b.get("time", ""),
        )[:_WINDOW_TRADING_DAYS]
        if len(window) < _WINDOW_TRADING_DAYS:
            continue
        day1_open = _coerce_float(window[0].get("open"))
        day5_close = _coerce_float(window[-1].get("close"))
        if day1_open is None or day1_open <= 0 or day5_close is None:
            continue
        returns.append((day5_close / day1_open - 1.0) * 100.0)

    return {
        "avg_return_pct": (
            round(sum(returns) / len(returns), 4) if returns else None
        ),
        "resolved": len(returns),
        "pool_size": len(symbols),
    }


async def _set_progress(
    db,
    d: Discussion,
    *,
    day1_opens: dict[str, float],
    daily_closes: dict[str, list[float | None]],
) -> None:
    """Persist D1-D4 outcome progress without finalizing the verdict."""
    values = {
        "day1_open_prices": day1_opens or None,
        "daily_close_prices": daily_closes or None,
    }
    await db.execute(
        update(Discussion)
        .where(Discussion.id == d.id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    d.day1_open_prices = day1_opens or None
    d.daily_close_prices = daily_closes or None


async def _set_verdict(
    db,
    d: Discussion,
    *,
    verdict: str,
    reason: str,
    day1_opens: dict[str, float],
    day5_closes: dict[str, float] | None = None,
    daily_closes: dict[str, list[float | None]] | None = None,
    pool_performance: dict[str, Any] | None = None,
) -> None:
    now = datetime.now(UTC)
    closes = day5_closes if day5_closes is not None else {}
    values: dict[str, Any] = {
        "verdict": verdict,
        "verdict_reason": reason,
        "verified_at": now,
        "day1_open_prices": day1_opens or None,
        "day5_close_prices": closes or None,
    }
    if daily_closes is not None:
        values["daily_close_prices"] = daily_closes or None
    if pool_performance is not None:
        values["pool_performance"] = pool_performance
    await db.execute(
        update(Discussion)
        .where(Discussion.id == d.id)
        .values(**values)
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
    if daily_closes is not None:
        d.daily_close_prices = daily_closes or None
    if pool_performance is not None:
        d.pool_performance = pool_performance

    # PR-B2: bump hit_count on every lesson cited by this discussion's
    # round contexts when the verdict is a winning band (legacy "win"
    # or new "big_win"). record_lesson_outcome is gated internally so
    # calling unconditionally is safe; failure only logs.
    # `is_experiment` rows are excluded for the same reason as the
    # post-mortem below: hit_count is the promotion eligibility signal, so a
    # win earned under non-production rules would push a lesson toward the
    # semantic tier that steers the live panel.
    if is_winning_verdict(verdict) and not is_experiment(d):
        try:
            from services.lesson_tier_service import (
                record_lesson_outcome,
            )

            await record_lesson_outcome(db, discussion_id=d.id)
        except Exception as exc:
            log.debug(
                "verify_discussion_outcome.record_lesson_outcome_failed",
                extra={"id": str(d.id), "error": str(exc)},
            )
    await maybe_run_live_post_mortem(db, d)
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


def _format_verdict_reason(
    band: str,
    winner_symbol: str | None,
    sym_cls,
    *,
    big_win_pct: float,
    win_pct: float,
    big_loss_pct: float,
) -> str:
    """Human-readable verdict_reason for the discussion sidebar.
    `sym_cls` is the OutcomeClassification of the winning symbol
    (None when classifications was empty — shouldn't happen for a
    written verdict but defensive anyway)."""
    if sym_cls is None or winner_symbol is None:
        return f"{band}: no classification details"
    if band == "big_loss":
        return (
            f"{winner_symbol} trough close {sym_cls.trough_pct:.2f}% "
            f"(≤ {big_loss_pct:g}% threshold)"
        )
    if band == "big_win":
        return (
            f"{winner_symbol} D5 close {sym_cls.day5_pct:+.2f}% " f"(≥ {big_win_pct:g}% threshold)"
        )
    if band == "win":
        return (
            f"{winner_symbol} D5 close {sym_cls.day5_pct:+.2f}% "
            f"(≥ {win_pct:g}% threshold; 期間最高 {sym_cls.peak_pct:+.2f}%)"
        )
    return (
        f"{winner_symbol} D5 close {sym_cls.day5_pct:+.2f}% — no band crossed "
        f"(期間最高 {sym_cls.peak_pct:+.2f}% / 最低 {sym_cls.trough_pct:+.2f}%)"
    )
