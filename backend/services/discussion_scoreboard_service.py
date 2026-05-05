"""Per-discussion scoreboard service: D1-D5 daily close vs day-1 open.

PR-C1 also exposes Brier score + reliability bucketing on top of
the same scoreboard computation, so PR-C2's calibration fitter
and the sweep aggregate dashboard can surface "did the synthesizer
say 0.8 confidence and was that right 80% of the time?" without a
second pass over OHLCV.

Builds the data behind the "對答案" UI on the discussion detail
page. For each recommended symbol, walks the 5 trading days
starting from the discussion's anchor date and reports:

Anchor selection mirrors `tasks/verify_discussion_outcome`: backtest
discussions (`as_of_date IS NOT NULL`) anchor to that date so the
post-window is graded against bars from `as_of_date` onward, not
from the discussion's `created_at` (which is the day the user ran
the backtest, typically months later). Live discussions keep the
`to_tw_date(created_at)` anchor.

  - day1_open: the open price on the first trading day at or after
    creation. Pinned in `discussion.day1_open_prices` once captured
    so a later upstream correction can't shift the baseline.
  - daily_closes: list of 5 close prices (or None for unresolved
    days when the cron caught the row before the window completed).
  - change_pcts: list of `(close - day1_open) / day1_open` per day.
  - days_resolved: count of non-None entries in daily_closes.

Two entry points:

  * `compute_scoreboard(db, discussion)` — pure read, returns the
    structured dict without writing. Used by the API endpoint when
    the persisted column is still NULL.

  * `persist_scoreboard(db, discussion)` — calls compute, writes
    `daily_close_prices` (and back-fills missing `day1_open_prices`
    entries), commits. Returns True only when ALL symbols have
    days_resolved=5 — the daily cron uses that signal to skip
    fully-resolved rows on the next tick.

Reads OHLCV from the DB archive (`ohlcv_daily`), which is populated
by the daily TW EOD ingest. No live waterfall — fresh deploys with
no archive simply return days_resolved=0 until ingest catches up.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion
from services.ingest.repository import read_ohlcv_range_autosession
from services.tw_trading_calendar import to_tw_date

log = logging.getLogger(__name__)

WINDOW_DAYS = 5
# Calendar-day lookahead from creation date to cover 5 trading days
# plus weekends + the rare TW holiday week. ~2× the trading-day count
# is the safe bound used by `verify_discussion_outcome`.
_LOOKAHEAD_CALENDAR_DAYS = 14


async def compute_scoreboard(
    db: AsyncSession,  # noqa: ARG001 (sig kept symmetric with persist_)
    discussion: Discussion,
) -> dict[str, Any]:
    """Read-only computation. See module docstring for the contract.

    Returns the dict shape directly — no DB writes — so the API
    endpoint can serve the on-demand path without a full session
    factory.
    """
    syms = _recommended_symbols(discussion)
    anchor_tw = _anchor_date(discussion)
    cached_opens: dict[str, float] = dict(discussion.day1_open_prices or {})

    rows: list[dict[str, Any]] = []
    for sym in syms:
        rows.append(
            await _compute_for_symbol(
                sym=sym,
                anchor_tw=anchor_tw,
                cached_open=cached_opens.get(sym),
            )
        )

    anchor_iso = anchor_tw.isoformat()
    return {
        "discussion_id": str(discussion.id),
        "anchor_date": anchor_iso,
        # Kept for backwards compatibility with frontends that haven't
        # adopted `anchor_date` yet. Same value as `anchor_date` in
        # backtest mode now (the live-mode value is unchanged).
        "created_at_tw_date": anchor_iso,
        "rows": rows,
    }


async def persist_scoreboard(
    db: AsyncSession, discussion: Discussion,
) -> bool:
    """Compute + persist. Returns True iff every recommended symbol
    has all 5 days resolved (caller can use that as the
    "skip-on-next-tick" signal).

    Atomic SQL UPDATE matches the pattern used elsewhere in this
    module's neighbours (`force_reset_status`, verifier writes) to
    avoid the SQLAlchemy 2.0 expunge-on-ORM-update gotcha that
    breaks `db.refresh(discussion)` under PostgreSQL.
    """
    payload = await compute_scoreboard(db, discussion)
    rows = payload["rows"]

    # Build the JSON column shape: {symbol: [c1, c2, c3, c4, c5]}.
    daily: dict[str, list[float | None]] = {
        r["symbol"]: r["daily_closes"] for r in rows
    }
    # Back-fill day1_open_prices for symbols the verifier hasn't
    # touched yet (e.g. manual discussions) so the existing
    # frontend `formatDiscussionTitle` rendering and the verdict
    # task have a stable baseline once they look at this row.
    existing_opens: dict[str, float] = dict(discussion.day1_open_prices or {})
    for r in rows:
        if r["day1_open"] is not None and r["symbol"] not in existing_opens:
            existing_opens[r["symbol"]] = r["day1_open"]

    fully_resolved = bool(rows) and all(
        r["days_resolved"] == WINDOW_DAYS for r in rows
    )

    # PR-C1: stage the daily_close_prices write before computing
    # Brier so the snapshot the function produces sees the freshly-
    # resolved closes. Scratch attribute on the in-memory row so
    # `compute_brier_for_discussion` (which reads off the ORM
    # instance, not the DB) finds the new closes without a refresh
    # round-trip.
    discussion.daily_close_prices = daily
    discussion.day1_open_prices = existing_opens or None

    brier_payload: dict[str, Any] | None = None
    if fully_resolved:
        brier_payload = compute_brier_for_discussion(discussion)

    update_values: dict[str, Any] = {
        "daily_close_prices": daily,
        "day1_open_prices": existing_opens or None,
    }
    if brier_payload is not None:
        update_values["brier_score"] = brier_payload["brier_score"]
        update_values["outcome_vector"] = brier_payload["outcome_vector"]
        discussion.brier_score = brier_payload["brier_score"]
        discussion.outcome_vector = brier_payload["outcome_vector"]
        # PR-C2 follow-up: persist calibrated_brier alongside raw so
        # the sweep aggregate + calibration audit can compare.
        # NULL is a valid value (means no calibrated_confidence
        # was present on the picks); we still write it to clear
        # any stale value from an earlier compute.
        update_values["calibrated_brier_score"] = brier_payload.get(
            "calibrated_brier_score",
        )
        discussion.calibrated_brier_score = brier_payload.get(
            "calibrated_brier_score",
        )

    await db.execute(
        update(Discussion)
        .where(Discussion.id == discussion.id)
        .values(**update_values)
        .execution_options(synchronize_session=False)
    )
    await db.commit()

    return fully_resolved


# ── internals ──────────────────────────────────────────────────────


def _anchor_date(discussion: Discussion) -> date:
    """The TW-local date the post-window is graded against.

    Backtest discussions anchor to `as_of_date` (the date the user
    is asking "what would the personas have said back then"), live
    discussions anchor to `to_tw_date(created_at)`. Mirrors the same
    selection in `tasks.verify_discussion_outcome` so both modules
    grade against the same window.
    """
    if discussion.as_of_date is not None:
        return discussion.as_of_date
    return to_tw_date(discussion.created_at)


def _recommended_symbols(discussion: Discussion) -> list[str]:
    """Pull recommended_symbols out of the conclusion JSON, dedup +
    strip. Returns [] for un-concluded discussions or malformed
    conclusions (defensive — synthesizer guard already coerces but
    this module shouldn't crash on a legacy row)."""
    conclusion = discussion.conclusion or {}
    raw = conclusion.get("recommended_symbols") or []
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for s in raw:
        sym = str(s).strip()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


async def _compute_for_symbol(
    *,
    sym: str,
    anchor_tw: date,
    cached_open: float | None,
) -> dict[str, Any]:
    """One symbol's scoreboard row. Reads OHLCV via the autosession
    helper (closed inside repository.py) so we don't have to thread
    a session through and risk holding it across the whole batch.

    Falls back to `tw_market_service.get_history` (TWSE → FinMind
    waterfall) when the DB archive has zero bars for this symbol —
    covers the case where the daily OHLCV cron had a transient
    failure for one symbol but the rest of the universe ingested
    fine. The fallback's own Redis cache (4h TTL) keeps repeated
    scoreboard reads cheap.
    """
    end = anchor_tw + timedelta(days=_LOOKAHEAD_CALENDAR_DAYS)
    bars: list[dict[str, Any]] = []
    try:
        bars = await read_ohlcv_range_autosession(
            "TW", sym, anchor_tw, end,
        )
    except Exception as exc:
        log.warning(
            "scoreboard.ohlcv_read_failed",
            extra={"symbol": sym, "error": str(exc)},
        )
        bars = []

    # Live fallback for symbols completely missing from the archive.
    # Only fires on `bars == []` so a partial window (e.g. 3 of 5
    # days ingested) doesn't burn an extra upstream call when we
    # already have what we need.
    if not bars:
        try:
            from services import tw_market_service
            live_bars = await tw_market_service.get_history(sym, months=1)
            # `get_history` returns bars with `time` ISO strings,
            # same shape as the repository helper. Constrain to
            # [anchor_tw, end] so the downstream filter still works.
            iso_end = end.isoformat()
            iso_start_chk = anchor_tw.isoformat()
            bars = [
                b for b in (live_bars or [])
                if iso_start_chk <= (b.get("time") or "") <= iso_end
            ]
            if bars:
                log.info(
                    "scoreboard.live_fallback_recovered",
                    extra={"symbol": sym, "bars": len(bars)},
                )
        except Exception as exc:
            log.warning(
                "scoreboard.live_fallback_failed",
                extra={"symbol": sym, "error": str(exc)},
            )

    # Filter to bars on or after the anchor date. The repository
    # helper already constrains by [start, end] but we re-check
    # defensively in case a future repository change widens the
    # bound semantics.
    iso_start = anchor_tw.isoformat()
    future = [b for b in bars if (b.get("time") or "") >= iso_start]
    window = future[:WINDOW_DAYS]

    day1_open: float | None = None
    if window:
        first_open = window[0].get("open")
        if isinstance(first_open, (int, float)):
            day1_open = float(first_open)
    if cached_open is not None:
        # Cached snapshot wins — stable baseline even when an
        # upstream correction shifts the bar later.
        day1_open = cached_open

    daily_closes: list[float | None] = []
    for i in range(WINDOW_DAYS):
        if i < len(window):
            c = window[i].get("close")
            daily_closes.append(float(c) if isinstance(c, (int, float)) else None)
        else:
            daily_closes.append(None)

    change_pcts: list[float | None] = []
    for c in daily_closes:
        if c is None or day1_open is None or day1_open <= 0:
            change_pcts.append(None)
        else:
            change_pcts.append(round((c - day1_open) / day1_open, 6))

    days_resolved = sum(1 for c in daily_closes if c is not None)

    return {
        "symbol": sym,
        "day1_open": day1_open,
        "daily_closes": daily_closes,
        "change_pcts": change_pcts,
        "days_resolved": days_resolved,
    }


# ── PR-C1: Brier score + reliability bucketing ──────────────────────


# Default win threshold mirrors the post_mortem service. Lifted to a
# parameter so unit tests can pin it without poking the runtime
# config plumbing.
DEFAULT_BRIER_THRESHOLD_PCT = 5.0


def compute_brier_for_discussion(
    discussion: Discussion,
    *,
    threshold_pct: float = DEFAULT_BRIER_THRESHOLD_PCT,
    window_days: int = WINDOW_DAYS,
) -> dict[str, Any] | None:
    """Compute the per-discussion Brier score from `conclusion.
    recommendations` and `daily_close_prices`.

    Returns:
      `{"brier_score": float,         # raw confidence
        "calibrated_brier_score": float | None,  # PR-C2 follow-up
        "outcome_vector": [{symbol, confidence, calibrated_confidence,
                            outcome_binary, peak_pct}, ...],
        "samples": int,
        "threshold_pct": float}`

    or `None` when:
      - the discussion has no `recommendations` (pre-PR-C0 row that
        slipped past the parser's fallback — defensive)
      - `daily_close_prices` is empty / missing
      - none of the recommendations have a fully-resolved D1-D5
        window (so every outcome would be skipped)

    `outcome_binary` per symbol = 1 iff the symbol's peak D1-D5
    change_pct is `>= threshold_pct`. Symbols with insufficient bars
    are skipped (not counted as 0) — caller's `samples` field tells
    the consumer how many actually contributed.

    PR-C2 follow-up: when a recommendation also carries a
    `calibrated_confidence` (set by the calibration apply step in
    `synthesize_conclusion` when the strategy has a fitted curve),
    a parallel `calibrated_brier_score` is computed. NULL when no
    recommendation carried a calibrated value — comparing the two
    requires both to exist so the metric is left absent rather
    than silently falling back to raw.
    """
    conclusion = discussion.conclusion or {}
    recommendations = conclusion.get("recommendations")
    if not isinstance(recommendations, list) or not recommendations:
        return None
    daily = discussion.daily_close_prices or {}
    opens = discussion.day1_open_prices or {}
    if not daily and not opens:
        return None

    outcome_vector: list[dict[str, Any]] = []
    losses: list[float] = []
    calibrated_losses: list[float] = []
    for entry in recommendations:
        if not isinstance(entry, dict):
            continue
        symbol = str(entry.get("symbol", "")).strip()
        if not symbol:
            continue
        try:
            confidence = float(entry.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        calibrated: float | None = None
        raw_calibrated = entry.get("calibrated_confidence")
        if raw_calibrated is not None:
            try:
                calibrated = max(0.0, min(1.0, float(raw_calibrated)))
            except (TypeError, ValueError):
                calibrated = None

        closes = daily.get(symbol) or []
        day1_open = opens.get(symbol)
        peak_pct = _peak_change_pct(
            closes, day1_open=day1_open, window_days=window_days,
        )
        if peak_pct is None:
            continue   # insufficient data — skip the symbol entirely
        outcome_binary = 1 if peak_pct >= threshold_pct else 0
        vector_entry: dict[str, Any] = {
            "symbol": symbol,
            "confidence": round(confidence, 4),
            "outcome_binary": outcome_binary,
            "peak_pct": round(peak_pct, 4),
        }
        if calibrated is not None:
            vector_entry["calibrated_confidence"] = round(calibrated, 4)
            calibrated_losses.append((calibrated - outcome_binary) ** 2)
        outcome_vector.append(vector_entry)
        losses.append((confidence - outcome_binary) ** 2)

    if not losses:
        return None

    # Calibrated Brier requires every contributing pick to have
    # had a calibrated value — partial coverage would let raw +
    # calibrated entries get averaged together which makes the
    # comparison meaningless. NULL when the coverage isn't
    # complete.
    calibrated_brier: float | None
    if calibrated_losses and len(calibrated_losses) == len(losses):
        calibrated_brier = round(
            sum(calibrated_losses) / len(calibrated_losses), 6,
        )
    else:
        calibrated_brier = None

    return {
        "brier_score": round(sum(losses) / len(losses), 6),
        "calibrated_brier_score": calibrated_brier,
        "outcome_vector": outcome_vector,
        "samples": len(losses),
        "threshold_pct": threshold_pct,
    }


def compute_reliability_buckets(
    pairs: list[tuple[float, int]],
    *,
    n_buckets: int = 10,
) -> list[dict[str, Any]]:
    """Slice `(confidence, outcome_binary)` pairs into `n_buckets`
    equal-width bins on [0, 1] and report each bin's mean predicted
    probability vs observed hit rate.

    Output: list of dicts in ascending bucket order
    `{bucket_lower, bucket_upper, mean_confidence, hit_rate, count}`.
    Empty buckets are included with `count=0` and NULL means/rates
    so the frontend renders a continuous diagram.

    Used by both the sweep aggregate dashboard (one chart per
    sweep) and PR-C2's isotonic regression fitter (sanity check
    that the curve really is monotonic in the right direction).
    """
    if n_buckets < 2:
        raise ValueError("n_buckets must be >= 2")
    width = 1.0 / n_buckets
    buckets: list[dict[str, Any]] = []
    for i in range(n_buckets):
        lo = i * width
        hi = (i + 1) * width if i < n_buckets - 1 else 1.0 + 1e-9
        in_bucket = [
            (c, o) for c, o in pairs
            if lo <= c < hi
        ]
        if in_bucket:
            mean_conf = sum(c for c, _ in in_bucket) / len(in_bucket)
            hit_rate = sum(o for _, o in in_bucket) / len(in_bucket)
            buckets.append({
                "bucket_lower": round(lo, 4),
                "bucket_upper": round(min(hi, 1.0), 4),
                "mean_confidence": round(mean_conf, 4),
                "hit_rate": round(hit_rate, 4),
                "count": len(in_bucket),
            })
        else:
            buckets.append({
                "bucket_lower": round(lo, 4),
                "bucket_upper": round(min(hi, 1.0), 4),
                "mean_confidence": None,
                "hit_rate": None,
                "count": 0,
            })
    return buckets


def _peak_change_pct(
    closes: list[float | None] | None,
    *,
    day1_open: float | None,
    window_days: int,
) -> float | None:
    """Best (max) D1-D`window_days` close-vs-day1-open percent move.

    Returns None when `day1_open` is missing/zero or none of the
    daily closes resolved — the caller treats that as "skip this
    symbol's contribution to the Brier sum".
    """
    if day1_open is None or not day1_open or not isinstance(closes, list):
        return None
    truncated = closes[:window_days]
    pcts = [
        (c - day1_open) / day1_open * 100
        for c in truncated
        if isinstance(c, (int, float))
    ]
    if not pcts:
        return None
    return max(pcts)
