"""Per-discussion scoreboard service: D1-D5 daily close vs day-1 open.

Debug mode (PR followup #426): callers can pass an empty
`debug_traces` list into `compute_scoreboard` to receive per-symbol
diagnostic dicts, and `build_scoreboard_debug_payload` rolls those
plus discussion-level eligibility / trading-window info into one
JSON-serialisable blob the `/scoreboard?debug=true` endpoint exposes
for "why is this discussion's scoreboard empty" debugging.

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
import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion
from services.ingest.repository import read_ohlcv_range_autosession
from services.outcome_classifier import (
    DEFAULT_BIG_LOSS_PCT,
    DEFAULT_BIG_WIN_DAY5_PCT,
    DEFAULT_WIN_PCT,
    band_to_binary,
    classify_outcome,
)
from services.tw_trading_calendar import to_tw_date

# Mirror of `tasks.score_discussion_outcomes._REFRESH_WINDOW_DAYS`.
# Kept here too so the debug payload can report cron eligibility
# without importing the task module (avoids a circular import path
# through `db.session` when this service is imported at startup).
_CRON_REFRESH_WINDOW_DAYS = 14

log = logging.getLogger(__name__)

WINDOW_DAYS = 5
# Calendar-day lookahead from creation date to cover 5 trading days
# plus weekends + the rare TW holiday week. ~2× the trading-day count
# is the safe bound used by `verify_discussion_outcome`.
_LOOKAHEAD_CALENDAR_DAYS = 14


async def compute_scoreboard(
    db: AsyncSession,  # noqa: ARG001 (sig kept symmetric with persist_)
    discussion: Discussion,
    *,
    debug_traces: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Read-only computation. See module docstring for the contract.

    Returns the dict shape directly — no DB writes — so the API
    endpoint can serve the on-demand path without a full session
    factory.

    When `debug_traces` is non-None, the function appends one dict
    per recommended symbol with per-step diagnostics (archive read
    count, live-fallback usage, window slice, day1_open source).
    Mutating the caller-supplied list keeps the return shape stable
    so existing callers don't have to unpack a tuple.
    """
    from services.discussion.symbol_names import resolve_display_name

    syms = _recommended_symbols(discussion)
    anchor_tw = _anchor_date(discussion)
    cached_opens: dict[str, float] = dict(discussion.day1_open_prices or {})

    rows: list[dict[str, Any]] = []
    for sym in syms:
        symbol_trace: dict[str, Any] | None = (
            {"symbol": sym} if debug_traces is not None else None
        )
        row = await _compute_for_symbol(
            sym=sym,
            anchor_tw=anchor_tw,
            cached_open=cached_opens.get(sym),
            debug_trace=symbol_trace,
        )
        row["name"] = resolve_display_name(discussion.market, sym)
        rows.append(row)
        if symbol_trace is not None:
            debug_traces.append(symbol_trace)

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
    debug_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One symbol's scoreboard row. Reads OHLCV via the autosession
    helper (closed inside repository.py) so we don't have to thread
    a session through and risk holding it across the whole batch.

    Live fallback hits the TWSE per-stock OHLCV endpoint directly
    (and FinMind as a backstop) when the DB archive has zero bars
    for the requested [anchor_tw, lookahead_end] window. We
    deliberately bypass `tw_market_service.get_history` here — that
    service's DB tier 2 treats up-to-5-days-stale archive bars as
    "fresh" and short-circuits without calling upstream, which is
    correct for a K-line chart use case but wrong for the
    scoreboard: when the archive is missing the anchor-week
    specifically (the daily ingest cron lagged or upstream was
    broken when it ran), we need a guaranteed upstream read.
    Successful fallback bars are upserted into `ohlcv_daily` so
    subsequent reads on this discussion serve directly from the
    archive without re-firing the fallback.
    """
    end = anchor_tw + timedelta(days=_LOOKAHEAD_CALENDAR_DAYS)
    if debug_trace is not None:
        debug_trace["lookahead_end"] = end.isoformat()
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
        if debug_trace is not None:
            debug_trace["archive_read_error"] = str(exc)

    if debug_trace is not None:
        debug_trace["archive_bars_count"] = len(bars)
        debug_trace["archive_dates"] = [b.get("time") for b in bars]

    # Live fallback for symbols completely missing from the archive.
    # Only fires on `bars == []` so a partial window (e.g. 3 of 5
    # days ingested) doesn't burn an extra upstream call when we
    # already have what we need.
    live_fallback_used = False
    if not bars:
        if debug_trace is not None:
            debug_trace["live_fallback_tried"] = True
        bars, source = await _live_fallback_fetch(
            sym=sym,
            anchor_tw=anchor_tw,
            end=end,
            debug_trace=debug_trace,
        )
        if bars:
            live_fallback_used = True
            log.info(
                "scoreboard.live_fallback_recovered",
                extra={"symbol": sym, "bars": len(bars), "source": source},
            )
            await _persist_fallback_bars_to_archive(sym, bars, source)
    elif debug_trace is not None:
        debug_trace["live_fallback_tried"] = False
        debug_trace["live_fallback_bars_count"] = 0

    # Filter to bars on or after the anchor date. The repository
    # helper already constrains by [start, end] but we re-check
    # defensively in case a future repository change widens the
    # bound semantics.
    iso_start = anchor_tw.isoformat()
    future = [b for b in bars if (b.get("time") or "") >= iso_start]
    window = future[:WINDOW_DAYS]

    day1_open: float | None = None
    day1_open_source = "none"
    if window:
        first_open = window[0].get("open")
        if isinstance(first_open, (int, float)):
            day1_open = float(first_open)
            day1_open_source = "live_fallback" if live_fallback_used else "archive"
    if cached_open is not None:
        # Cached snapshot wins — stable baseline even when an
        # upstream correction shifts the bar later.
        day1_open = cached_open
        day1_open_source = "cache"

    if debug_trace is not None:
        debug_trace["window_bars_count"] = len(window)
        debug_trace["window_dates"] = [b.get("time") for b in window]
        debug_trace["day1_open_source"] = day1_open_source

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
    if debug_trace is not None:
        debug_trace["days_resolved"] = days_resolved

    return {
        "symbol": sym,
        "day1_open": day1_open,
        "daily_closes": daily_closes,
        "change_pcts": change_pcts,
        "days_resolved": days_resolved,
    }


async def _live_fallback_fetch(
    *,
    sym: str,
    anchor_tw: date,
    end: date,
    debug_trace: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Pull bars in `[anchor_tw, end]` directly from upstream.

    TWSE STOCK_DAY returns one full calendar month per call, so we
    fan out across the months the window touches (typically one,
    occasionally two when the anchor lies in the last week of a
    month and the window crosses into the next). FinMind catches
    the TWSE-down case. Returns the filtered bars + which upstream
    provided them ("twse" / "finmind"), or `([], None)` if both
    upstreams produced nothing in the requested window.
    """
    from data.tw import finmind_connector as _fm
    from data.tw import twse_connector as _twse

    iso_anchor = anchor_tw.isoformat()
    iso_end = end.isoformat()

    months_to_fetch = sorted({
        date(anchor_tw.year, anchor_tw.month, 1),
        date(end.year, end.month, 1),
    })

    twse_bars: list[dict[str, Any]] = []
    twse_error: str | None = None
    seen_dates: set[str] = set()
    for first in months_to_fetch:
        try:
            m = await _twse.get_daily_ohlcv(sym, first)
            # Dedup by `time` field — TWSE returns the whole month
            # per call, so adjacent months overlap zero bars in
            # practice, but a defensive dedup keeps the filter
            # honest if anything ever ships duplicate rows.
            for r in (m or []):
                t = r.get("time") or ""
                if t and t not in seen_dates:
                    seen_dates.add(t)
                    twse_bars.append(r)
        except Exception as exc:
            twse_error = str(exc)
            log.warning(
                "scoreboard.live_fallback.twse_failed",
                extra={"symbol": sym, "month": first.isoformat(), "error": twse_error},
            )

    bars = [
        b for b in twse_bars
        if iso_anchor <= (b.get("time") or "") <= iso_end
    ]
    source: str | None = "twse" if bars else None

    if debug_trace is not None:
        debug_trace["live_fallback_source_twse_bars"] = len(twse_bars)
        if twse_error:
            debug_trace["live_fallback_twse_error"] = twse_error

    if not bars:
        try:
            fm_bars = await _fm.get_daily_ohlcv(sym, iso_anchor)
            bars = [
                b for b in (fm_bars or [])
                if iso_anchor <= (b.get("time") or "") <= iso_end
            ]
            if debug_trace is not None:
                debug_trace["live_fallback_source_finmind_bars"] = len(bars)
            if bars:
                source = "finmind"
        except Exception as exc:
            log.warning(
                "scoreboard.live_fallback.finmind_failed",
                extra={"symbol": sym, "error": str(exc)},
            )
            if debug_trace is not None:
                debug_trace["live_fallback_finmind_error"] = str(exc)

    if debug_trace is not None:
        debug_trace["live_fallback_bars_count"] = len(bars)
        debug_trace["live_fallback_source"] = source

    return bars, source


async def _persist_fallback_bars_to_archive(
    sym: str, bars: list[dict[str, Any]], source: str | None,
) -> None:
    """Write the fallback's bars into `ohlcv_daily` so the next
    scoreboard read for this discussion serves from the archive
    instead of refetching upstream. Best-effort — a failure here
    must not break the user-facing scoreboard render."""
    if not bars or not source:
        return
    try:
        from services.ingest.repository import (
            OhlcvBar,
            upsert_ohlcv_bars_autosession,
        )
        ohlcv_bars = [
            b for b in (
                OhlcvBar.from_connector_row("TW", sym, source, r)
                for r in bars
            )
            if b is not None
        ]
        if ohlcv_bars:
            await upsert_ohlcv_bars_autosession(ohlcv_bars)
    except Exception as exc:
        log.warning(
            "scoreboard.live_fallback.archive_write_failed",
            extra={"symbol": sym, "error": str(exc)},
        )


# ── debug payload (PR followup #426) ───────────────────────────────


async def build_scoreboard_debug_payload(
    discussion: Discussion,
    per_symbol_traces: list[dict[str, Any]],
) -> dict[str, Any]:
    """Roll per-symbol traces + discussion-level eligibility info +
    last cron-run snapshot into a single JSON-serialisable blob the
    `/scoreboard?debug=true` endpoint exposes.

    Mirrors the cron filter logic in
    `tasks.score_discussion_outcomes._do_run` so an operator can
    see at a glance whether THIS discussion would be picked up on
    the next tick (and if not, why).
    """
    # Avoid an import-time cycle: the task module imports this
    # service.
    from tasks.score_discussion_outcomes import (
        JOB_ID as CRON_JOB_ID,
    )
    from services.ingest.repository import get_health

    now = datetime.now(UTC)
    refresh_floor = now - timedelta(days=_CRON_REFRESH_WINDOW_DAYS)

    # SQLite-backed tests round-trip TIMESTAMPTZ columns as naive
    # datetimes even though the inserted values were aware. Coerce
    # to UTC here so the comparison below works portably; production
    # asyncpg-backed PostgreSQL already returns aware values.
    created_at = discussion.created_at
    if created_at is not None and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    conclusion_present = bool(discussion.conclusion)
    daily_close_missing = discussion.daily_close_prices is None
    within_refresh_window = (
        created_at is not None and created_at >= refresh_floor
    )
    eligible = conclusion_present and (
        daily_close_missing or within_refresh_window
    )
    skip_reason: str | None
    if not conclusion_present:
        skip_reason = "no_conclusion"
    elif not (daily_close_missing or within_refresh_window):
        skip_reason = "already_scored_and_outside_refresh_window"
    else:
        skip_reason = None

    raw_recommended = _recommended_symbols(discussion)
    anchor_tw = _anchor_date(discussion)

    daily = discussion.daily_close_prices
    if daily is None:
        daily_state = "null"
    elif not daily:
        daily_state = "empty_dict"
    else:
        daily_state = "populated"

    cron_health: dict[str, Any] | None = None
    try:
        health = await get_health(CRON_JOB_ID)
        if health is not None:
            cron_health = {
                "last_run_at":    health.last_run_at,
                "ok":             health.ok,
                "row_count":      health.row_count,
                "error":          health.error,
                "latest_data_ts": health.latest_data_ts,
            }
    except Exception as exc:
        log.warning(
            "scoreboard.debug.cron_health_lookup_failed",
            extra={"error": str(exc)},
        )

    return {
        "discussion": {
            "status":                    discussion.status,
            "created_at":                created_at.isoformat() if created_at else None,
            "as_of_date":                discussion.as_of_date.isoformat() if discussion.as_of_date else None,
            "anchor_date":               anchor_tw.isoformat(),
            "anchor_source":             "as_of_date" if discussion.as_of_date else "created_at_tw",
            "market":                    discussion.market,
            "conclusion_present":        conclusion_present,
            "recommended_symbols":       raw_recommended,
            "daily_close_prices_state":  daily_state,
            "daily_close_prices_keys":   sorted((daily or {}).keys()),
            "day1_open_prices_keys":     sorted((discussion.day1_open_prices or {}).keys()),
        },
        "cron_eligibility": {
            "eligible":                       eligible,
            "conclusion_present":             conclusion_present,
            "daily_close_missing":            daily_close_missing,
            "within_refresh_window":          within_refresh_window,
            "refresh_window_days":            _CRON_REFRESH_WINDOW_DAYS,
            "refresh_floor":                  refresh_floor.isoformat(),
            "skip_reason":                    skip_reason,
        },
        "trading_window": {
            "anchor_tw":          anchor_tw.isoformat(),
            "lookahead_calendar_days": _LOOKAHEAD_CALENDAR_DAYS,
            "lookahead_end":      (anchor_tw + timedelta(days=_LOOKAHEAD_CALENDAR_DAYS)).isoformat(),
            "window_days_target": WINDOW_DAYS,
        },
        "per_symbol":  per_symbol_traces,
        "last_cron_run": cron_health,
    }


# ── PR-C1: Brier score + reliability bucketing ──────────────────────


# Defaults mirror `services.outcome_classifier`. Lifted to keyword
# arguments so unit tests can pin them without poking the runtime
# config plumbing. Production callers leave them at the defaults; the
# 4-band classifier reads runtime config itself via its caller.


def compute_brier_for_discussion(
    discussion: Discussion,
    *,
    big_win_day5_pct: float = DEFAULT_BIG_WIN_DAY5_PCT,
    win_pct: float = DEFAULT_WIN_PCT,
    big_loss_pct: float = DEFAULT_BIG_LOSS_PCT,
    window_days: int = WINDOW_DAYS,
) -> dict[str, Any] | None:
    """Compute the per-discussion Brier score from `conclusion.
    recommendations` and `daily_close_prices`.

    Returns:
      `{"brier_score": float,         # raw confidence
        "calibrated_brier_score": float | None,  # PR-C2 follow-up
        "outcome_vector": [{symbol, confidence, calibrated_confidence,
                            outcome_binary, verdict_band, peak_pct,
                            day5_pct, trough_pct}, ...],
        "samples": int,
        "win_pct": float}`

    or `None` when:
      - the discussion has no `recommendations` (pre-PR-C0 row that
        slipped past the parser's fallback — defensive)
      - `daily_close_prices` is empty / missing
      - none of the recommendations have a fully-resolved D1-D5
        window (so every outcome would be skipped)

    `outcome_binary` per symbol stays binary (1 if classifier band is
    win or big_win, else 0) so Brier / calibration math stays
    comparable to historical rows. `verdict_band` adds the 4-band
    detail for the frontend; downstream code that wants the fine
    detail reads `verdict_band`, while the binary calibrator path
    keeps reading `outcome_binary`.

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
                calibrated_f = float(raw_calibrated)
            except (TypeError, ValueError):
                calibrated_f = float("nan")
            # Audit follow-up #3: explicit finite-float check.
            # Clamping NaN/inf via min/max would collapse a
            # pathological value to a corner (1.0 / 0.0) and let
            # it pollute the calibrated_brier sum with a
            # misleadingly precise number. Treating it as missing
            # instead trips the partial-coverage NULL guard so
            # the comparison metric stays meaningful.
            if math.isfinite(calibrated_f):
                calibrated = max(0.0, min(1.0, calibrated_f))

        closes = daily.get(symbol) or []
        day1_open = opens.get(symbol)
        if day1_open is None or day1_open <= 0:
            continue
        result = classify_outcome(
            d1_buy=day1_open,
            closes=list(closes[:window_days]),
            big_win_day5_pct=big_win_day5_pct,
            win_pct=win_pct,
            big_loss_pct=big_loss_pct,
        )
        if result is None:
            continue   # insufficient data — skip the symbol entirely
        outcome_binary = band_to_binary(result.band)
        vector_entry: dict[str, Any] = {
            "symbol": symbol,
            "confidence": round(confidence, 4),
            "outcome_binary": outcome_binary,
            "verdict_band": result.band,
            "peak_pct": result.peak_pct,
            "trough_pct": result.trough_pct,
            "day5_pct": result.day5_pct,
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
        "win_pct": win_pct,
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
