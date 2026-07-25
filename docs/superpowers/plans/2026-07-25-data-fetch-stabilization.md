# Data-Fetch Stabilization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The 04:00 Taipei daily discussion reads market data from the database (ingested the previous evening) instead of re-fetching TWSE/FinMind live, falling back to live calls only when the archive lacks the requested session.

**Architecture:** One new module (`read_session.py`) provides a trading-session resolver and an archive-first wrapper. The three DB-archived HTTP context blocks (screener, index, focus_briefs) call their existing services through the wrapper — first with `as_of=<settled session>`, falling back to `as_of=None` when the archive's answer is missing *or stale*. The builder's own `as_of` stays `None` so the row remains live-classified. Every block tags its source on `ctx["data_sources"]`.

**Tech Stack:** Python 3.12, SQLAlchemy async, pytest + AsyncMock (host venv `/tmp/fincept-test-venv/bin/pytest`), ruff.

**Spec:** `docs/superpowers/specs/2026-07-25-data-fetch-stabilization-design.md`

## Deviation from the spec (recorded during recon)

The spec listed `macro` among wrapped blocks with cadence-based staleness. Recon shows `_assemble_macro_block(as_of=...)` routes to **FRED HTTP with `observation_end`** either way — there is no local macro archive to prefer. Wrapping it would compare one HTTP call against another. Macro is therefore **excluded from the wrapper** and tagged `data_sources["macro"] = "live"`. The cadence mechanism (`max_lag_days`) is still built into the wrapper because focus-brief fundamentals are not daily either.

## Global Constraints

- Backend tests: `/tmp/fincept-test-venv/bin/python -m pytest` from `/opt/finceptweb99/backend` (pytest is NOT in the container).
- Lint: `/tmp/fincept-test-venv/bin/python -m ruff check --select E,W,F --ignore E501 <files>`.
- Work on a feature branch off `main`; one PR for the whole plan is fine (tasks = commits). CI green → standing authorization to self-merge. **Deploy needs explicit user approval — do not deploy.**
- Never use `date.today()` in tests — every calendar test is table-driven from fixed dates (weekend CI runs have burned this repo before).
- **Backtest mode (`as_of is not None`) must never gain a live fallback.** This is the load-bearing invariant (Task 5 pins it).
- US market paths stay untouched (live). Scope is the TW daily pipeline.

---

### Task 1: Session resolver + archive-first wrapper

**Files:**
- Create: `backend/services/discussion/context/read_session.py`
- Test: `backend/tests/test_read_session.py`

**Interfaces:**
- Produces: `resolve_read_session(now_tw: datetime | None = None) -> date`
- Produces: `async archive_first(call, *, session: date, answered_session, max_lag_days: int = 0) -> tuple[Any, str]` where `call: Callable[[date | None], Awaitable[Any]]`, `answered_session: Callable[[Any], date | None]`, and the returned `str` is one of `"archive" | "archive_stale" | "live_fallback"`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_read_session.py
"""Session resolver + archive-first wrapper for the daily discussion.

The resolver answers "which settled TW session should a live discussion
read from the archive". The wrapper implements archive-first-with-live-
fallback, where the fallback predicate is "did the archive answer FOR THE
REQUESTED SESSION" — an emptiness check is not enough because the archive
queries clamp `<= session` and silently return an older day when the
target is missing (the 11-day-stale broker-data abstention of 2026-05-20).
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from services.discussion.context.read_session import (
    archive_first,
    resolve_read_session,
)

TPE = ZoneInfo("Asia/Taipei")


# ── resolver: table-driven, never date.today() ─────────────────────

@pytest.mark.parametrize("now_tw, expected", [
    # 04:00 Friday — today's session hasn't traded: read Thursday.
    (datetime(2026, 7, 24, 4, 0, tzinfo=TPE), date(2026, 7, 23)),
    # 04:00 Monday — read the prior Friday.
    (datetime(2026, 7, 20, 4, 0, tzinfo=TPE), date(2026, 7, 17)),
    # 04:00 Saturday — read Friday.
    (datetime(2026, 7, 25, 4, 0, tzinfo=TPE), date(2026, 7, 24)),
    # 04:00 Sunday — still Friday.
    (datetime(2026, 7, 26, 4, 0, tzinfo=TPE), date(2026, 7, 24)),
    # 16:00 weekday — today's session has settled (post-14:30 publish).
    (datetime(2026, 7, 24, 16, 0, tzinfo=TPE), date(2026, 7, 24)),
    # 12:00 weekday — intraday, today not settled: read yesterday.
    (datetime(2026, 7, 24, 12, 0, tzinfo=TPE), date(2026, 7, 23)),
    # 16:00 Saturday — weekend afternoon still reads Friday.
    (datetime(2026, 7, 25, 16, 0, tzinfo=TPE), date(2026, 7, 24)),
])
def test_resolver_returns_most_recent_settled_session(now_tw, expected):
    assert resolve_read_session(now_tw) == expected


# ── wrapper ────────────────────────────────────────────────────────

def _answered(result):
    return result.get("session") if isinstance(result, dict) else None


@pytest.mark.asyncio
async def test_archive_answering_the_requested_session_wins():
    calls = []

    async def call(as_of):
        calls.append(as_of)
        return {"session": date(2026, 7, 23), "rows": [1]}

    result, source = await archive_first(
        call, session=date(2026, 7, 23), answered_session=_answered,
    )
    assert source == "archive"
    assert calls == [date(2026, 7, 23)]      # live path never invoked
    assert result["rows"] == [1]


@pytest.mark.asyncio
async def test_stale_archive_answer_falls_back_to_live():
    """`<= session` clamping returns an OLDER day when the target is
    missing. That must count as a miss, or week-old data is served
    silently and the fallback never fires."""
    async def call(as_of):
        if as_of is not None:
            return {"session": date(2026, 7, 18), "rows": ["old"]}
        return {"session": date(2026, 7, 23), "rows": ["fresh"]}

    result, source = await archive_first(
        call, session=date(2026, 7, 23), answered_session=_answered,
    )
    assert source == "live_fallback"
    assert result["rows"] == ["fresh"]


@pytest.mark.asyncio
async def test_empty_archive_falls_back_to_live():
    async def call(as_of):
        if as_of is not None:
            return []
        return {"session": date(2026, 7, 23), "rows": ["fresh"]}

    result, source = await archive_first(
        call, session=date(2026, 7, 23), answered_session=_answered,
    )
    assert source == "live_fallback"
    assert result["rows"] == ["fresh"]


@pytest.mark.asyncio
async def test_max_lag_days_accepts_a_recent_enough_answer():
    """Non-daily series (fundamentals snapshots) declare a cadence;
    an answer within it is NOT a miss."""
    async def call(as_of):
        assert as_of is not None, "live path must not be reached"
        return {"session": date(2026, 7, 21), "rows": [1]}

    result, source = await archive_first(
        call, session=date(2026, 7, 23),
        answered_session=_answered, max_lag_days=5,
    )
    assert source == "archive"


@pytest.mark.asyncio
async def test_live_failure_keeps_the_stale_archive_answer():
    """A stale archive answer beats no answer: if the live fallback
    raises, serve the stale data and say so."""
    async def call(as_of):
        if as_of is not None:
            return {"session": date(2026, 7, 18), "rows": ["old"]}
        raise RuntimeError("upstream down")

    result, source = await archive_first(
        call, session=date(2026, 7, 23), answered_session=_answered,
    )
    assert source == "archive_stale"
    assert result["rows"] == ["old"]


@pytest.mark.asyncio
async def test_both_paths_empty_raises_nothing_and_reports_fallback():
    async def call(as_of):
        return []

    result, source = await archive_first(
        call, session=date(2026, 7, 23), answered_session=_answered,
    )
    assert source == "live_fallback"
    assert result == []


@pytest.mark.asyncio
async def test_live_failure_with_empty_archive_reraises():
    """Nothing to serve — the exception must reach the block's
    record_error exactly as it does today. No new silent path."""
    async def call(as_of):
        if as_of is not None:
            return []
        raise RuntimeError("upstream down")

    with pytest.raises(RuntimeError):
        await archive_first(
            call, session=date(2026, 7, 23), answered_session=_answered,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /opt/finceptweb99/backend && /tmp/fincept-test-venv/bin/python -m pytest tests/test_read_session.py -q`
Expected: FAIL — `ModuleNotFoundError: services.discussion.context.read_session`

- [ ] **Step 3: Implement the module**

```python
# backend/services/discussion/context/read_session.py
"""Archive-first reads for the live daily discussion.

The daily discussion runs at 04:00 Taipei, hours after every TW ingest
job finished writing the previous session into the database — yet live
mode re-fetched everything over HTTP. These two helpers close that gap:

- `resolve_read_session` answers "which settled session should a live
  run read". At 04:00 that is the previous trading day.
- `archive_first` calls an existing dual-mode service with
  `as_of=<session>` first and falls back to the live path only when the
  archive's answer is missing or STALE. Staleness matters because the
  archive queries clamp `<= session`: when the target day is missing
  they return an older day rather than nothing, and serving that
  silently is exactly how a panel ends up abstaining against 11-day-old
  broker data (2026-05-20, see the design doc).

The builder's own `as_of` stays None — the discussion row must remain
live-classified (no 回測 badge, no backtest gating, no extra day of
clamping).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from services.tw_trading_calendar import prev_trading_day_estimate

log = logging.getLogger(__name__)

# TWSE publishes the settled session (STOCK_DAY_ALL) around 14:30
# Taipei; 15:00 leaves margin for slow publish days.
_SESSION_SETTLED_HOUR = 15


def resolve_read_session(now_tw: datetime | None = None) -> date:
    """The most recent settled TW trading session as of `now_tw`.

    Before 15:00 Taipei (or on a weekend) that is the previous trading
    day; from 15:00 on a weekday it is the day itself. Holidays are
    handled downstream: the archive clamps `<= session`, so an estimate
    landing on a holiday resolves to the prior real session — which the
    staleness predicate in `archive_first` must therefore tolerate via
    the caller-declared `max_lag_days` when it matters.
    """
    if now_tw is None:
        now_tw = datetime.now(ZoneInfo("Asia/Taipei"))
    today = now_tw.date()
    if today.weekday() < 5 and now_tw.hour >= _SESSION_SETTLED_HOUR:
        return today
    return prev_trading_day_estimate(today)


async def archive_first(
    call: Callable[[date | None], Awaitable[Any]],
    *,
    session: date,
    answered_session: Callable[[Any], date | None],
    max_lag_days: int = 0,
) -> tuple[Any, str]:
    """Call `call(session)`; fall back to `call(None)` when the archive
    did not answer for the requested session.

    Returns `(result, source)` with source one of:
      - "archive"       — archive answered within `max_lag_days` of
                          `session`
      - "live_fallback" — archive missing/stale, live path answered
                          (or both were empty; empty live result is
                          returned as-is for the block's existing
                          empty-handling)
      - "archive_stale" — archive was stale AND the live path failed;
                          the stale answer is served because stale
                          beats blind (blind spots cause abstention)

    A live-path exception with nothing usable from the archive is
    re-raised so the block's `record_error` fires exactly as today.
    """
    archive_result = await call(session)
    answered = answered_session(archive_result) if archive_result else None
    floor = session - timedelta(days=max_lag_days)
    if answered is not None and answered >= floor:
        return archive_result, "archive"

    try:
        live_result = await call(None)
    except Exception:
        if archive_result:
            log.warning(
                "read_session.archive_stale_served",
                extra={
                    "requested": session.isoformat(),
                    "answered": answered.isoformat() if answered else None,
                },
            )
            return archive_result, "archive_stale"
        raise
    return live_result, "live_fallback"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /opt/finceptweb99/backend && /tmp/fincept-test-venv/bin/python -m pytest tests/test_read_session.py -q`
Expected: PASS (14 tests)

- [ ] **Step 5: Lint + commit**

```bash
cd /opt/finceptweb99/backend
/tmp/fincept-test-venv/bin/python -m ruff check --select E,W,F --ignore E501 \
  services/discussion/context/read_session.py tests/test_read_session.py
cd /opt/finceptweb99
git add backend/services/discussion/context/read_session.py backend/tests/test_read_session.py
git commit -m "feat(context): session resolver + archive-first wrapper"
```

---

### Task 2: Screener block reads the archive first

**Files:**
- Modify: `backend/services/discussion/context/blocks/http.py` (`fetch_screener`, TW branch, ~lines 25-120)
- Test: `backend/tests/test_discussion_context_blocks.py` (append)

**Interfaces:**
- Consumes: `archive_first`, `resolve_read_session` from Task 1.
- Produces: `fetch_screener(..., read_session: date | None = None)` — new keyword-only param, default `None` = today's behaviour. Writes `ctx["data_sources"]["screener"]`.

Backtest screener rows (the `as_of=<date>` path in `tw_market_service._get_screener_backtest`) each carry `"as_of": last.ts.isoformat()` and `"data_source": "ohlcv_daily"`. The answered-session accessor is the **max** row `as_of` (the freshest session in the batch).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_discussion_context_blocks.py`)

```python
# ── archive-first live reads (data-fetch stabilization) ────────────


def _screener_row(sym: str, session: str) -> dict:
    return {
        "symbol": sym, "change_pct": 1.0, "price": 100.0,
        "volume": 2_000_000, "as_of": session,
        "data_source": "ohlcv_daily",
    }


@pytest.mark.asyncio
async def test_screener_live_mode_prefers_the_archive():
    """With `read_session` set and the archive answering for that
    session, the live TWSE path must never be called and the source
    tag must say so."""
    ctx = _new_ctx()
    session = date(2026, 7, 23)

    async def fake_screener(**kwargs):
        # Wrapper's archive attempt: as_of == session.
        assert kwargs.get("as_of") == session, (
            "live path reached — archive-first broken"
        )
        return [_screener_row("2330", "2026-07-23"),
                _screener_row("2454", "2026-07-23")]

    with patch(
        "services.tw_market_service.get_screener",
        new=AsyncMock(side_effect=fake_screener),
    ):
        await http.fetch_screener(
            ctx, market="TW", top_n=5, as_of=None,
            read_session=session, record_error=_record(ctx),
        )

    assert ctx["data_sources"]["screener"] == "archive"
    assert ctx["top_gainers"][0]["symbol"] == "2330"


@pytest.mark.asyncio
async def test_screener_stale_archive_falls_back_to_live():
    """Archive clamps `<= session` and answers with an older day →
    treated as a miss; the live path serves and is tagged."""
    ctx = _new_ctx()
    session = date(2026, 7, 23)

    async def fake_screener(**kwargs):
        if kwargs.get("as_of") is not None:
            return [_screener_row("2330", "2026-07-18")]   # stale
        return [_screener_row("2330", "2026-07-23")]        # live

    with patch(
        "services.tw_market_service.get_screener",
        new=AsyncMock(side_effect=fake_screener),
    ):
        await http.fetch_screener(
            ctx, market="TW", top_n=5, as_of=None,
            read_session=session, record_error=_record(ctx),
        )

    assert ctx["data_sources"]["screener"] == "live_fallback"


@pytest.mark.asyncio
async def test_screener_without_read_session_behaves_as_before():
    """Default param → exactly today's live behaviour, no tag."""
    ctx = _new_ctx()
    with patch(
        "services.tw_market_service.get_screener",
        new=AsyncMock(return_value=[_screener_row("2330", "2026-07-23")]),
    ) as mock:
        await http.fetch_screener(
            ctx, market="TW", top_n=5, as_of=None,
            record_error=_record(ctx),
        )
    assert mock.call_args.kwargs.get("as_of") is None
    assert "screener" not in ctx.get("data_sources", {})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /opt/finceptweb99/backend && /tmp/fincept-test-venv/bin/python -m pytest tests/test_discussion_context_blocks.py -q -k "archive or read_session or behaves_as_before"`
Expected: FAIL — `fetch_screener() got an unexpected keyword argument 'read_session'`

- [ ] **Step 3: Implement**

In `fetch_screener`, add the parameter and rework only the TW branch's fetch call. The signature becomes:

```python
async def fetch_screener(
    ctx: dict[str, Any],
    *,
    market: str,
    top_n: int,
    as_of: date | None,
    record_error: ErrorRecorder,
    read_session: date | None = None,
) -> None:
```

Replace the TW branch's single `get_screener` call (keep everything below it — sorting, compaction, session stamping, diagnostics — unchanged, operating on `rows`):

```python
        if market == "TW":
            from services import tw_market_service
            screener_diagnostic: dict[str, Any] = {}

            if as_of is None and read_session is not None:
                # Live mode, archive-first: the ingest cron wrote this
                # session hours ago; prefer it over a 04:00 TWSE call.
                from services.discussion.context.read_session import (
                    archive_first,
                )

                def _batch_session(batch: list[dict[str, Any]]) -> date | None:
                    stamps = [
                        r.get("as_of") for r in batch
                        if isinstance(r.get("as_of"), str)
                    ]
                    if not stamps:
                        return None
                    try:
                        return date.fromisoformat(max(stamps)[:10])
                    except ValueError:
                        return None

                async def _call(a: date | None) -> list[dict[str, Any]]:
                    return await tw_market_service.get_screener(
                        limit=200, min_volume=1_000_000, as_of=a,
                        diagnostic=screener_diagnostic if a is None else None,
                    )

                rows, source = await archive_first(
                    _call, session=read_session,
                    answered_session=_batch_session,
                )
                ctx.setdefault("data_sources", {})["screener"] = source
            else:
                rows = await tw_market_service.get_screener(
                    limit=200, min_volume=1_000_000, as_of=as_of,
                    diagnostic=screener_diagnostic if as_of is None else None,
                )
```

One more required adjustment in the same branch: `sess_default` currently keys off `as_of` alone. When the archive served (`read_session` path), the rows carry their own `as_of` stamp; extend the row compaction default so archive rows keep their stamped session:

```python
            sess_default = (
                as_of.isoformat() if as_of is not None
                else tw_quote_session()
            )
            # Archive rows are session closes stamped per-row; prefer
            # the row's own stamp (`as_of` in archive shape,
            # `actual_session` in live-recovery shape) over wall-clock.
            ctx["top_gainers"] = [
                _compact_screener_row(
                    r,
                    as_of_session=(
                        r.get("actual_session") or r.get("as_of")
                        or sess_default
                    ),
                    is_intraday=False,
                )
                for r in scored[:top_n]
            ]
```

(apply the same `r.get("actual_session") or r.get("as_of") or sess_default` expression to the `top_losers` comprehension).

- [ ] **Step 4: Run the new tests plus the whole block suite**

Run: `cd /opt/finceptweb99/backend && /tmp/fincept-test-venv/bin/python -m pytest tests/test_discussion_context_blocks.py -q`
Expected: PASS (all — the two pre-existing screener tests must stay green)

- [ ] **Step 5: Lint + commit**

```bash
cd /opt/finceptweb99/backend
/tmp/fincept-test-venv/bin/python -m ruff check --select E,W,F --ignore E501 \
  services/discussion/context/blocks/http.py tests/test_discussion_context_blocks.py
cd /opt/finceptweb99
git add backend/services/discussion/context/blocks/http.py backend/tests/test_discussion_context_blocks.py
git commit -m "feat(context): screener block reads the archive first in live mode"
```

---

### Task 3: Index block reads the archive first

**Files:**
- Modify: `backend/services/discussion/context/blocks/http.py` (`fetch_index`, TW branch, ~lines 160-255)
- Test: `backend/tests/test_discussion_context_blocks.py` (append)

**Interfaces:**
- Consumes: `archive_first` from Task 1.
- Produces: `fetch_index(..., read_session: date | None = None)`. Writes `ctx["data_sources"]["index"]`.

`_get_index_backtest` returns `{"symbol": "TAIEX", ..., "data_source": "ohlcv_daily", "as_of": <last bar time str>}` or `{}` when the archive has no bars. The answered-session accessor parses `result["as_of"][:10]`.

- [ ] **Step 1: Write the failing tests** (append)

```python
@pytest.mark.asyncio
async def test_index_live_mode_prefers_the_archive():
    ctx = _new_ctx()
    session = date(2026, 7, 23)

    async def fake_index(**kwargs):
        assert kwargs.get("as_of") == session, "live path reached"
        return {
            "symbol": "TAIEX", "price": 23000.0, "change_pct": -1.2,
            "data_source": "ohlcv_daily", "as_of": "2026-07-23",
            "history": [{"time": "2026-07-23", "close": 23000.0}],
        }

    with patch(
        "services.tw_market_service.get_index",
        new=AsyncMock(side_effect=fake_index),
    ):
        await http.fetch_index(
            ctx, market="TW", as_of=None,
            read_session=session, record_error=_record(ctx),
        )

    assert ctx["data_sources"]["index"] == "archive"
    assert ctx["index"]["price"] == 23000.0
    # Archive answer is a settled close, never intraday.
    assert ctx["index"]["is_intraday"] is False
    assert ctx["index"]["as_of_session"] == "2026-07-23"


@pytest.mark.asyncio
async def test_index_stale_archive_falls_back_to_live():
    ctx = _new_ctx()
    session = date(2026, 7, 23)

    async def fake_index(**kwargs):
        if kwargs.get("as_of") is not None:
            return {"symbol": "TAIEX", "price": 22000.0,
                    "data_source": "ohlcv_daily", "as_of": "2026-07-18"}
        return {"symbol": "TAIEX", "price": 23000.0,
                "data_source": "twse", "as_of": "2026-07-23"}

    with patch(
        "services.tw_market_service.get_index",
        new=AsyncMock(side_effect=fake_index),
    ):
        await http.fetch_index(
            ctx, market="TW", as_of=None,
            read_session=session, record_error=_record(ctx),
        )

    assert ctx["data_sources"]["index"] == "live_fallback"
    assert ctx["index"]["price"] == 23000.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /opt/finceptweb99/backend && /tmp/fincept-test-venv/bin/python -m pytest tests/test_discussion_context_blocks.py -q -k index`
Expected: FAIL — unexpected keyword argument `read_session`

- [ ] **Step 3: Implement**

Signature gains `read_session: date | None = None`. In the TW branch, replace the single `get_index` call and the session stamping:

```python
        if market == "TW":
            from services import tw_market_service

            source: str | None = None
            if as_of is None and read_session is not None:
                from services.discussion.context.read_session import (
                    archive_first,
                )

                def _index_session(result: dict[str, Any]) -> date | None:
                    stamp = result.get("as_of") if isinstance(result, dict) else None
                    if not isinstance(stamp, str):
                        return None
                    try:
                        return date.fromisoformat(stamp[:10])
                    except ValueError:
                        return None

                async def _call(a: date | None) -> dict[str, Any]:
                    return await tw_market_service.get_index(
                        history_days=30, as_of=a,
                    )

                index, source = await archive_first(
                    _call, session=read_session,
                    answered_session=_index_session,
                )
                ctx.setdefault("data_sources", {})["index"] = source
            else:
                index = await tw_market_service.get_index(
                    history_days=30, as_of=as_of,
                )
            if index:
                history = index.get("history") or []
                history_last = (
                    history[-1].get("time") if history else None
                )
                if as_of is not None:
                    index["as_of_session"] = as_of.isoformat()
                    index["is_intraday"] = False
                elif source in ("archive", "archive_stale"):
                    # Archive answer: a settled close from ohlcv_daily,
                    # stamped with the bar's own session.
                    index["as_of_session"] = str(index.get("as_of") or "")[:10]
                    index["is_intraday"] = False
                else:
                    index["is_intraday"] = tw_index_is_intraday()
                    index["as_of_session"] = tw_index_session()
                index["history_last_session"] = history_last
            ctx["index"] = index
```

- [ ] **Step 4: Run the block suite**

Run: `cd /opt/finceptweb99/backend && /tmp/fincept-test-venv/bin/python -m pytest tests/test_discussion_context_blocks.py -q`
Expected: PASS

- [ ] **Step 5: Lint + commit**

```bash
cd /opt/finceptweb99/backend
/tmp/fincept-test-venv/bin/python -m ruff check --select E,W,F --ignore E501 \
  services/discussion/context/blocks/http.py tests/test_discussion_context_blocks.py
cd /opt/finceptweb99
git add backend/services/discussion/context/blocks/http.py backend/tests/test_discussion_context_blocks.py
git commit -m "feat(context): index block reads the archive first in live mode"
```

---

### Task 4: Focus-briefs block reads the archive first

**Files:**
- Modify: `backend/services/discussion/context/blocks/http.py` (`fetch_focus_briefs`, ~lines 271-295)
- Test: `backend/tests/test_discussion_context_blocks.py` (append)

**Interfaces:**
- Consumes: `archive_first` from Task 1.
- Produces: `fetch_focus_briefs(..., read_session: date | None = None)`. Writes `ctx["data_sources"]["focus_briefs"]`.
- Note: `services/stock_report_service.py:126` also calls `fetch_focus_briefs` — it passes no `read_session`, so the default keeps its behaviour bit-for-bit. Do not modify that file.

The backtest brief (`_build_tw_focus_brief_backtest`) carries `brief["quote"]["as_of_session"]` = the last bar's own date. The answered-session accessor is the **min** across briefs (the weakest answer governs — one stale symbol means the batch didn't fully answer). Briefs without a quote yield `None` → treated as a miss.

`max_lag_days=3`: individual symbols can legitimately lack a bar for the exact session (suspension, new listing) while the batch is otherwise fresh; 3 calendar days tolerates a long weekend but still catches the 11-trading-day staleness class.

- [ ] **Step 1: Write the failing tests** (append)

```python
def _fake_brief(sym: str, session: str) -> dict:
    return {
        "symbol": sym,
        "quote": {"price": 100.0, "change_pct": 0.5,
                  "as_of_session": session, "is_intraday": False},
        "technicals": {"rsi14": 55.0, "as_of_session": session},
    }


@pytest.mark.asyncio
async def test_focus_briefs_live_mode_prefers_the_archive():
    ctx = _new_ctx()
    session = date(2026, 7, 23)

    async def fake_assemble(**kwargs):
        assert kwargs.get("as_of") == session, "live path reached"
        return [_fake_brief("2330", "2026-07-23"),
                _fake_brief("2454", "2026-07-23")]

    with patch(
        "services.discussion_service._assemble_focus_briefs",
        new=AsyncMock(side_effect=fake_assemble),
    ):
        await http.fetch_focus_briefs(
            ctx, market="TW", focus_symbols=["2330", "2454"], as_of=None,
            read_session=session, record_error=_record(ctx),
        )

    assert ctx["data_sources"]["focus_briefs"] == "archive"
    assert len(ctx["focus_briefs"]) == 2


@pytest.mark.asyncio
async def test_focus_briefs_one_stale_symbol_within_lag_still_archive():
    """A single symbol answering 2 days back (suspension) is within
    max_lag_days=3 — not a batch miss."""
    ctx = _new_ctx()
    session = date(2026, 7, 23)

    async def fake_assemble(**kwargs):
        assert kwargs.get("as_of") == session, "live path reached"
        return [_fake_brief("2330", "2026-07-23"),
                _fake_brief("9999", "2026-07-21")]

    with patch(
        "services.discussion_service._assemble_focus_briefs",
        new=AsyncMock(side_effect=fake_assemble),
    ):
        await http.fetch_focus_briefs(
            ctx, market="TW", focus_symbols=["2330", "9999"], as_of=None,
            read_session=session, record_error=_record(ctx),
        )

    assert ctx["data_sources"]["focus_briefs"] == "archive"


@pytest.mark.asyncio
async def test_focus_briefs_stale_batch_falls_back_to_live():
    ctx = _new_ctx()
    session = date(2026, 7, 23)

    async def fake_assemble(**kwargs):
        if kwargs.get("as_of") is not None:
            return [_fake_brief("2330", "2026-07-10")]   # 13 days stale
        return [_fake_brief("2330", "2026-07-23")]

    with patch(
        "services.discussion_service._assemble_focus_briefs",
        new=AsyncMock(side_effect=fake_assemble),
    ):
        await http.fetch_focus_briefs(
            ctx, market="TW", focus_symbols=["2330"], as_of=None,
            read_session=session, record_error=_record(ctx),
        )

    assert ctx["data_sources"]["focus_briefs"] == "live_fallback"
    assert ctx["focus_briefs"][0]["quote"]["as_of_session"] == "2026-07-23"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /opt/finceptweb99/backend && /tmp/fincept-test-venv/bin/python -m pytest tests/test_discussion_context_blocks.py -q -k focus_briefs`
Expected: FAIL — unexpected keyword argument `read_session`

- [ ] **Step 3: Implement**

```python
async def fetch_focus_briefs(
    ctx: dict[str, Any],
    *,
    market: str,
    focus_symbols: list[str] | None,
    as_of: date | None,
    record_error: ErrorRecorder,
    read_session: date | None = None,
) -> None:
    """Per-focus-symbol mini analyst report. Skipped when no focus
    symbols. Each brief includes quote / 52w bands / RSI / moving
    averages so personas can cite real numbers instead of guessing
    from headlines."""
    if not focus_symbols:
        return
    try:
        from services.discussion_service import _assemble_focus_briefs

        if as_of is None and read_session is not None and market == "TW":
            from services.discussion.context.read_session import (
                archive_first,
            )

            def _batch_session(briefs: list[dict[str, Any]]) -> date | None:
                """Weakest answer governs — one unanswered symbol means
                the batch did not fully answer for the session."""
                stamps: list[date] = []
                for b in briefs:
                    raw = (b.get("quote") or {}).get("as_of_session")
                    if not isinstance(raw, str):
                        return None
                    try:
                        stamps.append(date.fromisoformat(raw[:10]))
                    except ValueError:
                        return None
                return min(stamps) if stamps else None

            async def _call(a: date | None) -> list[dict[str, Any]]:
                return await _assemble_focus_briefs(
                    market=market, symbols=list(focus_symbols), as_of=a,
                )

            briefs, source = await archive_first(
                _call, session=read_session,
                answered_session=_batch_session, max_lag_days=3,
            )
            ctx.setdefault("data_sources", {})["focus_briefs"] = source
            ctx["focus_briefs"] = briefs
        else:
            ctx["focus_briefs"] = await _assemble_focus_briefs(
                market=market,
                symbols=list(focus_symbols),
                as_of=as_of,
            )
    except Exception as exc:
        record_error("focus_briefs", exc)
```

- [ ] **Step 4: Run the block suite**

Run: `cd /opt/finceptweb99/backend && /tmp/fincept-test-venv/bin/python -m pytest tests/test_discussion_context_blocks.py -q`
Expected: PASS

- [ ] **Step 5: Lint + commit**

```bash
cd /opt/finceptweb99/backend
/tmp/fincept-test-venv/bin/python -m ruff check --select E,W,F --ignore E501 \
  services/discussion/context/blocks/http.py tests/test_discussion_context_blocks.py
cd /opt/finceptweb99
git add backend/services/discussion/context/blocks/http.py backend/tests/test_discussion_context_blocks.py
git commit -m "feat(context): focus-briefs block reads the archive first in live mode"
```

---

### Task 5: Builder wiring + the backtest-never-falls-back invariant

**Files:**
- Modify: `backend/services/discussion/context/builder.py` (~lines 278-300, the `asyncio.gather` over the four http blocks)
- Test: `backend/tests/test_discussion_context_blocks.py` (append)

**Interfaces:**
- Consumes: `resolve_read_session` (Task 1); the `read_session` params (Tasks 2-4).
- Produces: live TW builds carry `ctx["data_sources"]` with keys `screener`, `index`, `focus_briefs` (when focus symbols exist) and `macro: "live"`.

- [ ] **Step 1: Write the failing tests** (append)

```python
@pytest.mark.asyncio
async def test_builder_live_tw_passes_read_session_to_blocks(db_session):
    """Live TW build: the three DB-archived blocks receive the resolved
    settled session; macro is tagged live (FRED has no local archive)."""
    seen: dict[str, object] = {}

    async def spy_screener(ctx, **kwargs):
        seen["screener"] = kwargs.get("read_session")

    async def spy_index(ctx, **kwargs):
        seen["index"] = kwargs.get("read_session")

    async def spy_macro(ctx, **kwargs):
        seen["macro_read_session"] = kwargs.get("read_session", "absent")

    async def spy_briefs(ctx, **kwargs):
        seen["focus_briefs"] = kwargs.get("read_session")

    with patch.object(http, "fetch_screener", new=spy_screener), \
         patch.object(http, "fetch_index", new=spy_index), \
         patch.object(http, "fetch_macro", new=spy_macro), \
         patch.object(http, "fetch_focus_briefs", new=spy_briefs):
        ctx = await build_market_context(
            db_session, market="TW", focus_symbols=["2330"], as_of=None,
        )

    from services.discussion.context.read_session import resolve_read_session
    expected = resolve_read_session()
    assert seen["screener"] == expected
    assert seen["index"] == expected
    assert seen["focus_briefs"] == expected
    # Macro never gets a read_session — no local archive exists.
    assert seen["macro_read_session"] == "absent"
    assert ctx["data_sources"]["macro"] == "live"


@pytest.mark.asyncio
async def test_builder_backtest_never_passes_read_session(db_session):
    """THE invariant: backtest mode gets no archive-first machinery —
    its strict `as_of` path must stay byte-identical, because a live
    fallback here would let replays see the future and invalidate
    every backtest-derived conclusion."""
    seen: dict[str, object] = {}

    async def spy_screener(ctx, **kwargs):
        seen["read_session"] = kwargs.get("read_session")
        seen["as_of"] = kwargs.get("as_of")

    async def spy_noop(ctx, **kwargs):
        pass

    with patch.object(http, "fetch_screener", new=spy_screener), \
         patch.object(http, "fetch_index", new=spy_noop), \
         patch.object(http, "fetch_macro", new=spy_noop), \
         patch.object(http, "fetch_focus_briefs", new=spy_noop):
        await build_market_context(
            db_session, market="TW", focus_symbols=[],
            as_of=date(2026, 6, 12),
        )

    assert seen["read_session"] is None
    assert seen["as_of"] is not None


@pytest.mark.asyncio
async def test_backtest_block_call_never_reaches_live_path():
    """Belt to the builder test's braces, at block level: with `as_of`
    set, `fetch_screener` must never call the service with
    `as_of=None` even when the archive returns nothing — an exploding
    double, not a call-count assertion."""
    ctx = _new_ctx()

    async def exploding_when_live(**kwargs):
        if kwargs.get("as_of") is None:
            raise AssertionError("backtest reached the live path")
        return []

    with patch(
        "services.tw_market_service.get_screener",
        new=AsyncMock(side_effect=exploding_when_live),
    ):
        await http.fetch_screener(
            ctx, market="TW", top_n=5, as_of=date(2026, 6, 12),
            record_error=_record(ctx),
        )

    assert ctx["errors"] == []          # the double did not fire
    assert ctx["top_gainers"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /opt/finceptweb99/backend && /tmp/fincept-test-venv/bin/python -m pytest tests/test_discussion_context_blocks.py -q -k builder or backtest_block`
Expected: the two builder tests FAIL (`read_session` never passed / `data_sources` missing); the block-level test already PASSES (nothing routes backtest through the wrapper) — it is a regression tripwire.

- [ ] **Step 3: Implement**

In `builder.py`, immediately before the `asyncio.gather` (after `await _progress("fetching_market_data")`):

```python
    # Live TW runs read the archive first: every ingest job finished
    # hours before the 04:00 discussion, so the settled session is
    # already in the DB. `info_cutoff` stays None — the row must remain
    # live-classified; only the blocks' read target changes.
    read_session = None
    if info_cutoff is None and market == "TW":
        from services.discussion.context.read_session import (
            resolve_read_session,
        )
        read_session = resolve_read_session()
        # FRED macro has no local archive — its as_of path is still
        # outbound HTTP — so it is honestly tagged live rather than
        # wrapped.
        ctx.setdefault("data_sources", {})["macro"] = "live"

    await asyncio.gather(
        http.fetch_screener(
            ctx, market=market, top_n=top_n,
            as_of=info_cutoff, record_error=record_error,
            read_session=read_session,
        ),
        http.fetch_index(
            ctx, market=market, as_of=info_cutoff, record_error=record_error,
            read_session=read_session,
        ),
        http.fetch_macro(
            ctx, as_of=info_cutoff, record_error=record_error,
        ),
        http.fetch_focus_briefs(
            ctx, market=market, focus_symbols=focus_symbols,
            as_of=info_cutoff, record_error=record_error,
            read_session=read_session,
        ),
    )
```

- [ ] **Step 4: Run the full block + context suites**

Run: `cd /opt/finceptweb99/backend && /tmp/fincept-test-venv/bin/python -m pytest tests/test_discussion_context_blocks.py tests/test_read_session.py -q`
Expected: PASS

- [ ] **Step 5: Run the wider discussion suites (regression sweep)**

Run: `cd /opt/finceptweb99/backend && /tmp/fincept-test-venv/bin/python -m pytest tests/ -q -k "discussion or context or focus or screener"`
Expected: PASS — `stock_report_service` and every existing caller uses the default `read_session=None`, so nothing else changes behaviour.

- [ ] **Step 6: Lint + commit**

```bash
cd /opt/finceptweb99/backend
/tmp/fincept-test-venv/bin/python -m ruff check --select E,W,F --ignore E501 \
  services/discussion/context/builder.py tests/test_discussion_context_blocks.py
cd /opt/finceptweb99
git add backend/services/discussion/context/builder.py backend/tests/test_discussion_context_blocks.py
git commit -m "feat(context): live TW builds read the archive first; backtest invariant pinned"
```

---

### Task 6: R1 — honest health outcomes for the FinMind market-wide run

**Files:**
- Modify: `backend/finmind/scripts/run_due.py` (~lines 324-357 plus a new pure function near `_failed_health_summary`)
- Test: `backend/tests/test_run_due_outcome.py` (create)

**Interfaces:**
- Produces: `classify_run_outcome(outcomes) -> tuple[bool, str | None]` in `run_due.py` — `(ok, error_summary)`.

**Why:** the current rule is `ok = (no failed chunks)`. Observed effect over five straight days: the 07:10 run that wrote 15-17k rows reports **failed** (one chunk of ~30 timed out) while the 09:30/14:00 runs that wrote 0 rows (nothing due) report **ok**. The monitoring signal is inverted at both ends. New semantics:

| Condition | ok | error |
|---|---|---|
| no chunks due (idle) | True | `"idle: nothing due"` |
| no failed chunks | True | None |
| some failed, rows written | True | `"partial: <summary>"` |
| some failed, zero rows | False | summary |

`record_health` keeps its `ok: bool` API — partial success is honestly `ok=True` with the failure summary carried in `error` where the dashboard already displays it.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_run_due_outcome.py
"""Health-outcome semantics for the FinMind market-wide run.

Observed inversion (5 consecutive days in production): the run that
wrote 15k+ rows reported `failed` because one chunk of ~30 timed out,
while the runs that wrote nothing reported `ok` because nothing was
due. Any consumer gating on this signal is misled in both directions.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "finmind"))

from scripts.run_due import classify_run_outcome  # noqa: E402


def _outcome(status: str, rows: int, dataset: str = "ds", error: str | None = None):
    return SimpleNamespace(
        chunk=SimpleNamespace(dataset_code=dataset, symbol=None,
                              range_start="2026-07-01", range_end="2026-07-24"),
        result=SimpleNamespace(status=status, rows_written=rows, error=error),
    )


def test_idle_run_is_ok_and_labeled():
    ok, err = classify_run_outcome([])
    assert ok is True
    assert err == "idle: nothing due"


def test_clean_run_is_ok():
    ok, err = classify_run_outcome([_outcome("done", 500)])
    assert ok is True
    assert err is None


def test_partial_failure_with_rows_written_is_ok_with_summary():
    """One timed-out chunk must not label a 15k-row run as failed."""
    ok, err = classify_run_outcome([
        _outcome("done", 15_000),
        _outcome("failed", 0, dataset="TaiwanStockPER", error="timeout"),
    ])
    assert ok is True
    assert err is not None and err.startswith("partial:")
    assert "TaiwanStockPER" in err


def test_total_failure_is_failed():
    ok, err = classify_run_outcome([
        _outcome("failed", 0, error="quota"),
        _outcome("failed", 0, error="quota"),
    ])
    assert ok is False
    assert err is not None and not err.startswith("partial:")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /opt/finceptweb99/backend && /tmp/fincept-test-venv/bin/python -m pytest tests/test_run_due_outcome.py -q`
Expected: FAIL — `ImportError: cannot import name 'classify_run_outcome'`

- [ ] **Step 3: Implement**

In `run_due.py`, add below `_failed_health_summary`:

```python
def classify_run_outcome(outcomes) -> tuple[bool, str | None]:
    """(ok, error_summary) for the market-wide health record.

    The naive rule `ok = no failed chunks` inverts the signal at both
    ends: a run writing 15k rows with one timed-out chunk of ~30 read
    as `failed`, while an idle run (nothing due) read as a clean `ok`
    indistinguishable from real work. Partial success is success —
    with the failure summary carried in `error` so the dashboard still
    shows what went wrong — and idleness is labeled so a
    zero-row `ok` can never masquerade as a productive run.
    """
    if not outcomes:
        return True, "idle: nothing due"
    summary = _failed_health_summary(outcomes)
    if summary is None:
        return True, None
    rows = sum(o.result.rows_written for o in outcomes)
    if rows > 0:
        return True, f"partial: {summary}"
    return False, summary
```

Then replace the recording site (currently `health_error = _failed_health_summary(outcomes)` / `ok=health_error is None`):

```python
    rows = sum(o.result.rows_written for o in outcomes)
    ok, health_error = classify_run_outcome(outcomes)
    if args.tw_only:
        await _record_tw_marketwide_health(
            ok=ok,
            row_count=rows,
            error=health_error,
        )
    return EXIT_OK if ok else EXIT_CHUNK_FAILURE
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /opt/finceptweb99/backend && /tmp/fincept-test-venv/bin/python -m pytest tests/test_run_due_outcome.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Lint + commit**

```bash
cd /opt/finceptweb99/backend
/tmp/fincept-test-venv/bin/python -m ruff check --select E,W,F --ignore E501 \
  finmind/scripts/run_due.py tests/test_run_due_outcome.py
cd /opt/finceptweb99
git add backend/finmind/scripts/run_due.py backend/tests/test_run_due_outcome.py
git commit -m "fix(finmind): partial success is not failed; idle runs are labeled"
```

---

### Task 7: R2 — content-level archive freshness monitor

**Files:**
- Create: `backend/tasks/monitor_archive_freshness.py`
- Modify: `backend/tasks/scheduler.py` (one `add_job` block, alongside the other Cron jobs)
- Test: `backend/tests/test_monitor_archive_freshness.py` (create)

**Interfaces:**
- Consumes: `prev_trading_day_estimate` (existing), `record_health` (existing: `services.ingest.repository`), models `OhlcvDaily`, `TwInstitutionalDaily`, `TwMarginDaily`.
- Produces: `async run()`; pure helper `stale_datasets(latest: dict[str, date | None], expected: date) -> list[str]`; job id `"monitor_archive_freshness"`.

**Why:** job-level health lies (Task 6 fixed one instance; the class remains). The only trustworthy freshness signal is the data itself: `max(ts)` per table compared against the expected settled session. Scheduled at **19:00 UTC (03:00 Taipei)** — after every evening ingest, one hour before the 04:00 discussion — so a gap is on record before the discussion would hit its live fallback.

Datasets covered (verified models; the registry dict makes additions one-line):

| key | table | filter |
|---|---|---|
| `ohlcv_tw` | `OhlcvDaily` | `market="TW"`, symbol NOT LIKE `\_%` |
| `taiex` | `OhlcvDaily` | `market="TW"`, `symbol="_TAIEX"` |
| `institutional_tw` | `TwInstitutionalDaily` | — |
| `margin_tw` | `TwMarginDaily` | — |

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_monitor_archive_freshness.py
"""Content-level freshness: read max(ts) per dataset table instead of
trusting job health records, which have reported `failed` on runs that
wrote 15k rows and `ok` on runs that wrote none."""
from datetime import date

from tasks.monitor_archive_freshness import stale_datasets


def test_fresh_archive_reports_nothing():
    latest = {
        "ohlcv_tw": date(2026, 7, 24),
        "taiex": date(2026, 7, 24),
        "institutional_tw": date(2026, 7, 24),
        "margin_tw": date(2026, 7, 24),
    }
    assert stale_datasets(latest, expected=date(2026, 7, 24)) == []


def test_lagging_dataset_is_named_with_its_lag():
    latest = {
        "ohlcv_tw": date(2026, 7, 24),
        "taiex": date(2026, 7, 24),
        "institutional_tw": date(2026, 7, 21),   # 3 days behind
        "margin_tw": date(2026, 7, 24),
    }
    stale = stale_datasets(latest, expected=date(2026, 7, 24))
    assert stale == ["institutional_tw: 2026-07-21 (expected 2026-07-24)"]


def test_empty_table_is_stale_not_a_crash():
    latest = {"ohlcv_tw": None}
    stale = stale_datasets(latest, expected=date(2026, 7, 24))
    assert stale == ["ohlcv_tw: empty (expected 2026-07-24)"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /opt/finceptweb99/backend && /tmp/fincept-test-venv/bin/python -m pytest tests/test_monitor_archive_freshness.py -q`
Expected: FAIL — `ModuleNotFoundError: tasks.monitor_archive_freshness`

- [ ] **Step 3: Implement the task**

```python
# backend/tasks/monitor_archive_freshness.py
"""Content-level freshness check on the TW archive tables.

Job-level ingest health has proven unreliable in both directions
(`failed` on a 15k-row run, `ok` on zero-row runs), so this job asks
the only witness that cannot lie: the data. For each dataset table it
reads `max(ts)` and compares against the expected settled session.

Scheduled 19:00 UTC = 03:00 Taipei — after the evening ingest window
(all TW ingest lands by 19:30 Taipei) and one hour before the 04:00
daily discussion, so a gap is recorded before the discussion would hit
its archive-first live fallback.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from db.session import AsyncSessionLocal
from models.ohlcv_daily import OhlcvDaily
from models.tw_chip_metrics import TwInstitutionalDaily, TwMarginDaily
from services.ingest.repository import record_health
from services.tw_trading_calendar import prev_trading_day_estimate

log = logging.getLogger(__name__)

JOB_ID = "monitor_archive_freshness"


def stale_datasets(
    latest: dict[str, date | None], expected: date,
) -> list[str]:
    """Datasets whose newest row predates the expected session, as
    human-readable findings. Pure so the policy is table-testable."""
    findings: list[str] = []
    for key, newest in latest.items():
        if newest is None:
            findings.append(f"{key}: empty (expected {expected})")
        elif newest < expected:
            findings.append(f"{key}: {newest} (expected {expected})")
    return findings


async def _collect_latest() -> dict[str, date | None]:
    async with AsyncSessionLocal() as db:
        def _as_date(v):
            return v.date() if hasattr(v, "date") else v

        ohlcv = await db.scalar(
            select(func.max(OhlcvDaily.ts)).where(
                OhlcvDaily.market == "TW",
                OhlcvDaily.symbol.not_like(r"\_%"),
            )
        )
        taiex = await db.scalar(
            select(func.max(OhlcvDaily.ts)).where(
                OhlcvDaily.market == "TW",
                OhlcvDaily.symbol == "_TAIEX",
            )
        )
        inst = await db.scalar(select(func.max(TwInstitutionalDaily.ts)))
        margin = await db.scalar(select(func.max(TwMarginDaily.ts)))
        return {
            "ohlcv_tw": _as_date(ohlcv) if ohlcv else None,
            "taiex": _as_date(taiex) if taiex else None,
            "institutional_tw": _as_date(inst) if inst else None,
            "margin_tw": _as_date(margin) if margin else None,
        }


async def run() -> None:
    now_tw = datetime.now(ZoneInfo("Asia/Taipei"))
    # At 03:00 Taipei the expected newest session is the previous
    # trading day — the one the evening ingest wrote.
    expected = prev_trading_day_estimate(now_tw.date())
    latest = await _collect_latest()
    findings = stale_datasets(latest, expected)
    fresh = len(latest) - len(findings)
    if findings:
        log.warning(
            "monitor_archive_freshness.stale",
            extra={"expected": expected.isoformat(), "findings": findings},
        )
    await record_health(
        JOB_ID,
        ok=not findings,
        row_count=fresh,
        error="; ".join(findings) if findings else None,
        latest_data_ts=expected,
    )
```

- [ ] **Step 4: Register the schedule**

In `backend/tasks/scheduler.py`, following the exact pattern of the neighbouring Cron jobs (e.g. `promote_lessons`), add:

```python
    from tasks import monitor_archive_freshness
    scheduler.add_job(
        monitor_archive_freshness.run,
        CronTrigger(hour=19, minute=0, timezone="UTC"),
        id="monitor_archive_freshness",
        replace_existing=True,
    )
```

(Match the surrounding code's import placement and any shared error-wrapper the other jobs use — copy the `promote_lessons` registration block's exact shape.)

- [ ] **Step 5: Run tests + the scheduler suite**

Run: `cd /opt/finceptweb99/backend && /tmp/fincept-test-venv/bin/python -m pytest tests/test_monitor_archive_freshness.py -q && /tmp/fincept-test-venv/bin/python -m pytest tests/ -q -k scheduler`
Expected: PASS

- [ ] **Step 6: Lint + commit**

```bash
cd /opt/finceptweb99/backend
/tmp/fincept-test-venv/bin/python -m ruff check --select E,W,F --ignore E501 \
  tasks/monitor_archive_freshness.py tasks/scheduler.py tests/test_monitor_archive_freshness.py
cd /opt/finceptweb99
git add backend/tasks/monitor_archive_freshness.py backend/tasks/scheduler.py backend/tests/test_monitor_archive_freshness.py
git commit -m "feat(monitoring): content-level archive freshness check at 03:00 Taipei"
```

---

### Task 8: R3 — SEC EDGAR contact email (ops, needs user input)

**Files:**
- Modify: `/opt/finceptweb99/.env` (add `SEC_EDGAR_USER_AGENT_EMAIL=<user-provided>`)

No code change: `tasks/ingest_announcements_us.py` already reads the setting and already maps 403 to the correct hint. The value is empty, so SEC rejects every request — `ingest_announcements_us` has failed 90/90 runs over four days.

- [ ] **Step 1: Ask the user for a contact email** (SEC requires a real one in the User-Agent). **Do not invent one.** If the user defers, mark this task skipped in the PR description and move on.
- [ ] **Step 2: Append `SEC_EDGAR_USER_AGENT_EMAIL=<email>` to `/opt/finceptweb99/.env`.**
- [ ] **Step 3: Do NOT restart anything** — the value is picked up at the next backend/scheduler container recreation, which happens at the next approved deploy. Note in the PR that the fix activates on deploy.

---

### Task 9: PR + post-merge production acceptance

- [ ] **Step 1: Push the branch, open a PR** titled `feat(context): archive-first reads for the daily discussion + honest ingest health`. Body must cover: the archive-first design (link the spec), the macro deviation, the backtest invariant, R1 semantics table, R2 schedule, R3 status.
- [ ] **Step 2: CI green → merge** (standing authorization). **Do not deploy — ask the user.**
- [ ] **Step 3: After the user approves deploy and it completes**, verify in production (green tests are not the acceptance gate):

```bash
cd /opt/finceptweb99
# 1. The freshness monitor ran / can run:
docker-compose exec -T scheduler python -c "
import asyncio
from tasks.monitor_archive_freshness import run
asyncio.run(run())"
# 2. Trigger one real TW discussion via the existing admin path, then:
docker-compose exec -T postgres psql -U fincept -d finceptweb -c "
SELECT id, created_at,
       context->'data_sources' AS data_sources
FROM discussions
WHERE auto_run IS TRUE AND as_of_date IS NULL
ORDER BY created_at DESC LIMIT 1;"
```

Expected: `data_sources` shows `screener/index/focus_briefs = "archive"` and `macro = "live"`. Any `live_fallback` names a real ingest gap — record it as an R2 finding rather than a failure of this work. (If the `context` column doesn't carry `data_sources` because ctx pruning strips it, read it instead from the discussion's stored round-1 context JSON in `discussion_contexts` — same key.)

---

## Self-review notes

- **Spec coverage:** resolver (Task 1), wrapper + staleness predicate (Task 1), three DB-archived blocks (Tasks 2-4), builder + `data_sources` + backtest invariant (Task 5), R1 (Task 6), R2 (Task 7), R3 (Task 8), production acceptance (Task 9). Macro cadence clause replaced by the recorded deviation (no local archive exists).
- **Type consistency:** `archive_first` returns `tuple[Any, str]` everywhere; `read_session: date | None = None` on all three blocks; accessors return `date | None`.
- **Acceptance caveat:** Task 9's SQL assumes the ctx JSON lands in a queryable column; the fallback location is given inline.
