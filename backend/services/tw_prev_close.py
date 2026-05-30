"""TW prev_close resolution — pulled out of ``tw_market_service`` (PR-B
of the long-running TW-service split).

The four helpers here are the chain that ``fetch_quote_waterfall`` uses
to back-fill ``raw["prev_close"]`` when the upstream EOD payload doesn't
carry one (TWSE realtime never does; FinMind's bars[-2] does):

  ``_archive_last2_closes``    Tier 1 — read the last (date, close) pairs
                               from the local ``ohlcv_daily`` archive
                               (Redis-cached 4 h)
  ``_pick_prev_from_pairs``    Pure picker — decides whether the upstream
                               close is the same session as the latest
                               archived bar (use bar[-2]) or a fresh tick
                               that hasn't landed yet (use bar[-1])
  ``_finmind_prev_close``      Tier 2 — live FinMind fallback when the
                               archive is empty / stale (>7 d old)
  ``_resolve_prev_close``      Orchestrator — Tier 1 → Tier 2

The +992% / +996% bug class that TWSE's `Change` field used to produce
for KY-listed stocks is gone once `prev_close` is sourced from this
chain instead of trusting the upstream's pre-computed delta.

Kept distinct from ``fetch_quote_waterfall`` (which still lives in
``tw_market_service``) because:

  - the four helpers are pure prev_close mechanics with no dependency
    on TWSE / TWSE-MIS session timing
  - ``fetch_quote_waterfall`` needs ``_is_tw_market_open`` and
    ``_finmind_preferred``, both of which are shared with ``get_quote``
    and ``get_history`` and patched from those code paths by the test
    suite — moving them would require updating ~5 test files

``tw_market_service`` re-exports every name here, so existing callers
(notably ``test_tw_market_service_waterfall.py`` which directly invokes
``svc._resolve_prev_close``) keep working unchanged.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import data.tw.finmind_connector as finmind
from cache.redis_cache import (
    cache_get_json,
    cache_set_json,
    key_archive_last2_tw,
    key_prev_close_finmind_tw,
)

log = logging.getLogger(__name__)


async def _archive_last2_closes(symbol: str) -> list[tuple[str, float]]:
    """Return up to the last 2 (date, close) pairs from `ohlcv_daily`,
    ascending. Cached in Redis 4h. Empty list when the archive has
    nothing for the symbol.

    Caller decides which one is "prev_close" by comparing the
    upstream's currently-reported close — see `_resolve_prev_close`.
    """
    cache_key = key_archive_last2_tw(symbol)
    cached = await cache_get_json(cache_key)
    if cached is not None:
        try:
            return [(d, float(c)) for d, c in cached]
        except (TypeError, ValueError):
            pass
    try:
        from services.ingest.repository import read_ohlcv_range_autosession
        today = date.today()
        start = today - timedelta(days=14)
        bars = await read_ohlcv_range_autosession("TW", symbol, start, today)
    except Exception as exc:
        log.warning(
            "tw.quote.prev_close_lookup_failed",
            extra={"symbol": symbol, "error": str(exc)},
        )
        return []
    cleaned: list[tuple[str, float]] = []
    for b in bars:
        ts = b.get("time")
        cl = b.get("close")
        if ts is None or cl is None:
            continue
        try:
            cleaned.append((str(ts), float(cl)))
        except (TypeError, ValueError):
            continue
    if not cleaned:
        return []
    last2 = cleaned[-2:]
    await cache_set_json(
        cache_key,
        [[d, c] for d, c in last2],
        4 * 3600,
    )
    return last2


def _pick_prev_from_pairs(
    pairs: list[tuple[str, float]], upstream_close: float | None,
) -> float | None:
    """Shared logic: given an ascending list of (date, close) pairs,
    pick the right "previous" relative to upstream_close.

      - upstream_close ≈ pairs[-1].close → caller is serving the same
        close as the latest archived/finmind session. Previous is
        pairs[-2].
      - upstream_close differs → caller has a fresh tick that hasn't
        landed in the daily archive yet. Previous is pairs[-1].
    """
    if not pairs:
        return None
    latest_close = pairs[-1][1]
    same_session = (
        upstream_close is not None
        and abs(float(upstream_close) - latest_close) < 0.01
    )
    if same_session and len(pairs) >= 2:
        return pairs[-2][1]
    if same_session:
        return None
    return latest_close


async def _finmind_prev_close(
    symbol: str, upstream_close: float | None,
) -> float | None:
    """Live FinMind fallback for prev_close: fires when ohlcv_daily
    has nothing fresh for the symbol. Caches result in Redis 4 h so
    only the first quote-of-the-day pays the FinMind round-trip.

    Without this, KY-listed stocks (whose archive bars are sometimes
    missing because the cron either hasn't ingested them yet or
    stopped early on a TWSE 429) silently leave 昨收 blank — and the
    UI can only show the un-bounded upstream change as +996%."""
    cache_key = key_prev_close_finmind_tw(symbol)
    cached = await cache_get_json(cache_key)
    if cached is not None:
        try:
            pairs = [(str(d), float(c)) for d, c in cached]
        except (TypeError, ValueError):
            pairs = []
        if pairs:
            return _pick_prev_from_pairs(pairs, upstream_close)
    try:
        start = (date.today() - timedelta(days=10)).isoformat()
        bars = await finmind.get_daily_ohlcv(symbol, start)
    except Exception as exc:
        log.warning(
            "tw.quote.finmind_prev_close_failed",
            extra={"symbol": symbol, "error": str(exc)},
        )
        return None
    pairs: list[tuple[str, float]] = []
    for b in bars or []:
        d = b.get("date") or b.get("time")
        c = b.get("close")
        if d is None or c is None:
            continue
        try:
            pairs.append((str(d), float(c)))
        except (TypeError, ValueError):
            continue
    if not pairs:
        return None
    last2 = pairs[-2:]
    await cache_set_json(
        cache_key, [[d, c] for d, c in last2], 4 * 3600,
    )
    return _pick_prev_from_pairs(last2, upstream_close)


async def _resolve_prev_close(
    symbol: str, upstream_close: float | None,
) -> float | None:
    """Pick the correct prior-session close.

    Tier 1 — `ohlcv_daily` archive (fast path, no upstream call).
    Tier 2 — FinMind live daily OHLCV (when archive is empty or its
             latest bar is >7 days stale).

    The "right" prev depends on whether `upstream_close` is a live
    tick from today OR a stale snapshot of the last closed session
    (weekends, holidays, pre-market). See `_pick_prev_from_pairs` for
    the disambiguation rule.
    """
    last2 = await _archive_last2_closes(symbol)
    if last2:
        latest_date_iso, _ = last2[-1]
        # ETFs that recently went ex-distribution (e.g. 00713 dropping
        # from ~73 to ~53) leave the cron's last-ingested bar far
        # older than today, so comparing today's 52.85 against
        # months-old 73.71 yields a -28% headline. >7 days old =
        # treat as stale and let the FinMind tier take over.
        try:
            latest_date = date.fromisoformat(str(latest_date_iso)[:10])
        except (TypeError, ValueError):
            latest_date = None
        if latest_date is None or (date.today() - latest_date).days > 7:
            log.warning(
                "tw.quote.archive_stale_or_unparseable",
                extra={"symbol": symbol, "latest_archive_date": latest_date_iso},
            )
        else:
            picked = _pick_prev_from_pairs(last2, upstream_close)
            if picked is not None:
                return picked
    # Tier 2: archive missing OR stale OR same_session-with-only-one-bar
    # → ask FinMind directly. Cached 4 h to keep the per-symbol cost
    # at 1 round-trip per day.
    return await _finmind_prev_close(symbol, upstream_close)
