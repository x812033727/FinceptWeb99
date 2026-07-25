# Robust Fetch & Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unified fetch-failure semantics (with the buyback 422→TWSE fallback as proving case), same-day evening chip re-probe, and TimescaleDB compression for the big tables — no row is ever deleted.

**Architecture:** Track 1 adds a catalog-driven 4xx fallback inside `finmind/ingest/runner.ingest_chunk` and an outcome taxonomy (`not_yet_published` vs `gap`) to the chip ingest tasks via a pure classifier + a small `TaskOutcome.ok` extension. Track 2 is two scheduler entries reusing the same idempotent `run()`s. Track 3 is two Alembic migrations (compression policies; shareholding hypertable conversion) plus one prerequisite fix (TAIEX-TR append-only) and a rehearsal script.

**Tech Stack:** Python 3.12, SQLAlchemy async, Alembic, TimescaleDB 2.26.3 (community license — compression supported, verified `SHOW timescaledb.license` = `timescale`), pytest via host venv.

**Spec:** `docs/superpowers/specs/2026-07-25-robust-fetch-store-design.md`

## Global Constraints

- Backend tests: `cd <worktree>/backend && /tmp/fincept-test-venv/bin/python -m pytest ...` (host venv; pytest is NOT in containers).
- Lint: `/tmp/fincept-test-venv/bin/python -m ruff check --select E,W,F --ignore E501 <files>`.
- Never `date.today()` / wall-clock in tests — classifiers take explicit dates.
- **No row deletion anywhere.** Compression only.
- Migrations must be rehearsed against an ephemeral `timescale/timescaledb:2.26.3-pg16` container before merge (standing R6 rule, Timescale flavor because both migrations use Timescale DDL).
- Implementation work must NOT deploy: deploy is user-gated and additionally blocked until the running veto-relaxation experiment completes.
- Feature branch off `main`; one PR; CI green → standing self-merge authorization.

---

### Task 1: Catalog fallback lookup

**Files:**
- Modify: `backend/finmind/dataset_catalog.py` (append below `all_entries()`)
- Test: `backend/finmind/tests/test_dataset_catalog_fallback.py` (create)

**Interfaces:**
- Produces: `fallback_source_for(dataset_code: str) -> str | None` — the catalog's `fallback_source` for that dataset, `None` when the dataset is unknown or has no fallback.

- [ ] **Step 1: Write the failing test**

```python
# backend/finmind/tests/test_dataset_catalog_fallback.py
"""Catalog lookup used by the runner's 4xx fallback routing."""
from finmind.dataset_catalog import fallback_source_for


def test_buyback_falls_back_to_twse():
    # The proving case: TaiwanStockBuyBack has 422'd daily on FinMind
    # while a registered TWSE self-crawl fetcher sat unused.
    assert fallback_source_for("TaiwanStockBuyBack") == "twse"


def test_unknown_dataset_has_no_fallback():
    assert fallback_source_for("NoSuchDataset") is None


def test_dataset_without_fallback_returns_none():
    # Crypto datasets are FinMind/binance-native; entries whose
    # fallback_source is None must not fabricate one.
    from finmind.dataset_catalog import all_entries
    no_fb = next(
        (e.dataset_code for _, e in all_entries() if e.fallback_source is None),
        None,
    )
    if no_fb is not None:
        assert fallback_source_for(no_fb) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd <worktree>/backend && /tmp/fincept-test-venv/bin/python -m pytest finmind/tests/test_dataset_catalog_fallback.py -q`
Expected: FAIL — `ImportError: cannot import name 'fallback_source_for'`

- [ ] **Step 3: Implement**

```python
# append to backend/finmind/dataset_catalog.py, below all_entries()
_FALLBACK_BY_CODE: dict[str, str | None] | None = None


def fallback_source_for(dataset_code: str) -> str | None:
    """The catalog's registered fallback source for a dataset, or None.

    Used by the ingest runner's 4xx routing: a permanent client error
    from the primary source retries against this source in the same
    chunk instead of failing the dataset forever (TaiwanStockBuyBack
    spent months at zero rows with a working TWSE fetcher registered
    right here).
    """
    global _FALLBACK_BY_CODE
    if _FALLBACK_BY_CODE is None:
        _FALLBACK_BY_CODE = {
            entry.dataset_code: entry.fallback_source
            for _, entry in all_entries()
        }
    return _FALLBACK_BY_CODE.get(dataset_code)
```

- [ ] **Step 4: Run to verify pass** — same command, expected 3 passed.
- [ ] **Step 5: Lint + commit**

```bash
cd <worktree>/backend
/tmp/fincept-test-venv/bin/python -m ruff check --select E,W,F --ignore E501 \
  finmind/dataset_catalog.py finmind/tests/test_dataset_catalog_fallback.py
cd <worktree>
git add backend/finmind/dataset_catalog.py backend/finmind/tests/test_dataset_catalog_fallback.py
git commit -m "feat(finmind): catalog fallback lookup for 4xx routing"
```

---

### Task 2: Runner routes permanent 4xx to the registered fallback source

**Files:**
- Modify: `backend/finmind/ingest/runner.py` (the fetch/transform `try` block inside `ingest_chunk`, ~lines 308-341)
- Test: `backend/finmind/tests/test_runner_fallback.py` (create)

**Interfaces:**
- Consumes: `fallback_source_for` (Task 1); `resolve_client` (`finmind/ingest/selfcrawl/__init__.py:181`).
- Produces: behavioural — a chunk whose primary fetch raises `httpx.HTTPStatusError` with a 4xx status (except 429) retries via the fallback client and, on success, completes `done` with the fallback's rows. 429 and 5xx keep today's behaviour (fail → existing backoff machinery). A fallback that raises (including `NotImplementedError` from an unregistered handler) fails the chunk with the ORIGINAL primary error.

- [ ] **Step 1: Write the failing test**

```python
# backend/finmind/tests/test_runner_fallback.py
"""4xx → fallback-source routing in ingest_chunk.

Doctrine (spec Track 1): 4xx other than 429 is permanent for that
request shape — retrying FinMind is pointless, but a registered
self-crawl fallback can serve the same dataset. 429/5xx stay on the
existing retry/backoff path.
"""
from datetime import date
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from finmind.ingest import runner as R


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://api.finmindtrade.com/api/v4/data")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


class _Primary:
    async def fetch(self, dataset_code, symbol, start, end):
        raise _http_error(422)


class _Primary429:
    async def fetch(self, dataset_code, symbol, start, end):
        raise _http_error(429)


class _Fallback:
    def __init__(self):
        self.calls = []

    async def fetch(self, dataset_code, symbol, start, end):
        self.calls.append(dataset_code)
        return [{"stock_id": "2330", "date": "2026-07-24"}]


@pytest.mark.asyncio
async def test_422_routes_to_fallback_and_completes(monkeypatch):
    fb = _Fallback()
    monkeypatch.setattr(R, "_resolve_fallback_client",
                        lambda code, source: fb)
    result = await R._fetch_with_fallback(
        _Primary(), dataset_code="TaiwanStockBuyBack", symbol=None,
        range_start=date(2026, 7, 17), range_end=date(2026, 7, 24),
        source="finmind",
    )
    assert fb.calls == ["TaiwanStockBuyBack"]
    assert result[0]["stock_id"] == "2330"


@pytest.mark.asyncio
async def test_429_does_not_fall_back(monkeypatch):
    fb = _Fallback()
    monkeypatch.setattr(R, "_resolve_fallback_client",
                        lambda code, source: fb)
    with pytest.raises(httpx.HTTPStatusError):
        await R._fetch_with_fallback(
            _Primary429(), dataset_code="TaiwanStockBuyBack", symbol=None,
            range_start=date(2026, 7, 17), range_end=date(2026, 7, 24),
            source="finmind",
        )
    assert fb.calls == []


@pytest.mark.asyncio
async def test_no_fallback_registered_reraises_original(monkeypatch):
    monkeypatch.setattr(R, "_resolve_fallback_client",
                        lambda code, source: None)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await R._fetch_with_fallback(
            _Primary(), dataset_code="TaiwanStockBuyBack", symbol=None,
            range_start=date(2026, 7, 17), range_end=date(2026, 7, 24),
            source="finmind",
        )
    assert exc_info.value.response.status_code == 422


@pytest.mark.asyncio
async def test_fallback_failure_reraises_original_error(monkeypatch):
    class _BrokenFallback:
        async def fetch(self, *a, **k):
            raise NotImplementedError("no handler")

    monkeypatch.setattr(R, "_resolve_fallback_client",
                        lambda code, source: _BrokenFallback())
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await R._fetch_with_fallback(
            _Primary(), dataset_code="TaiwanStockBuyBack", symbol=None,
            range_start=date(2026, 7, 17), range_end=date(2026, 7, 24),
            source="finmind",
        )
    assert exc_info.value.response.status_code == 422
```

- [ ] **Step 2: Run to verify it fails**

Run: `/tmp/fincept-test-venv/bin/python -m pytest finmind/tests/test_runner_fallback.py -q`
Expected: FAIL — `AttributeError: ... has no attribute '_fetch_with_fallback'`

- [ ] **Step 3: Implement**

Add to `runner.py` (above `ingest_chunk`):

```python
def _is_permanent_client_error(exc: BaseException) -> bool:
    """4xx other than 429: the request shape is rejected — retrying the
    same source is pointless, but a different source may serve it."""
    import httpx

    return (
        isinstance(exc, httpx.HTTPStatusError)
        and 400 <= exc.response.status_code < 500
        and exc.response.status_code != 429
    )


def _resolve_fallback_client(dataset_code: str, source: str):
    """The SourceClient for this dataset's catalog fallback, or None.

    Only meaningful when the PRIMARY source raised: a dataset already
    running on its fallback (active_source != 'finmind') has nowhere
    further to go.
    """
    if source != "finmind":
        return None
    from finmind.dataset_catalog import fallback_source_for
    from finmind.ingest.selfcrawl import resolve_client

    fb = fallback_source_for(dataset_code)
    if fb is None or fb == source:
        return None
    try:
        return resolve_client(fb)
    except KeyError:
        return None


async def _fetch_with_fallback(
    upstream,
    *,
    dataset_code: str,
    symbol: str | None,
    range_start: date,
    range_end: date,
    source: str,
) -> list[dict[str, Any]]:
    """Primary fetch, with catalog-fallback routing on permanent 4xx.

    A fallback that itself raises (including NotImplementedError from a
    dataset the fallback client has no handler for) re-raises the
    ORIGINAL primary error — the operator should see the 422 that
    started it, not the fallback's stack.
    """
    try:
        return await upstream.fetch(dataset_code, symbol, range_start, range_end)
    except Exception as primary_exc:
        if not _is_permanent_client_error(primary_exc):
            raise
        fallback = _resolve_fallback_client(dataset_code, source)
        if fallback is None:
            raise
        log.warning(
            "ingest_chunk: %s 4xx on %s — routing to catalog fallback",
            dataset_code, source,
        )
        try:
            return await fallback.fetch(
                dataset_code, symbol, range_start, range_end
            )
        except Exception:
            raise primary_exc
```

Then in `ingest_chunk`, replace the single fetch line:

```python
        raw_rows = await upstream.fetch(
            dataset_code, symbol, range_start, range_end
        )
```

with:

```python
        raw_rows = await _fetch_with_fallback(
            upstream, dataset_code=dataset_code, symbol=symbol,
            range_start=range_start, range_end=range_end, source=source,
        )
```

- [ ] **Step 4: Run the new tests + the existing runner/script suites**

Run: `/tmp/fincept-test-venv/bin/python -m pytest finmind/tests/test_runner_fallback.py finmind/tests/ -q`
Expected: all pass (434 existing + 4 new).

- [ ] **Step 5: Lint + commit**

```bash
/tmp/fincept-test-venv/bin/python -m ruff check --select E,W,F --ignore E501 \
  finmind/ingest/runner.py finmind/tests/test_runner_fallback.py
git add backend/finmind/ingest/runner.py backend/finmind/tests/test_runner_fallback.py
git commit -m "feat(finmind): permanent 4xx routes to the catalog fallback source"
```

---

### Task 3: `TaskOutcome.ok` — a task can succeed-with-alarm

**Files:**
- Modify: `backend/tasks/_runner.py` (the `TaskOutcome` dataclass ~line 60 and the success-path `record_health` call after `body()` returns)
- Test: `backend/tests/test_task_runner_outcome_ok.py` (create)

**Interfaces:**
- Produces: `TaskOutcome(row_count=..., status=..., ok: bool = True)`. When `ok=False`, the runner records `record_health(job_id, ok=False, row_count=..., error=status)` but does NOT arm the failure backoff (`record_failure` is not called) — a data gap is an alarm, not a transport failure to retry into.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_task_runner_outcome_ok.py
"""TaskOutcome.ok=False: alarm without backoff.

`gap` (a past session that should have data but doesn't) must surface
as not-ok on the dashboard, but arming the transport backoff would be
wrong — the next scheduled run is exactly what heals it.
"""
from unittest.mock import AsyncMock

import pytest

from tasks._runner import TaskOutcome, run_guarded


def _collabs(**overrides):
    base = dict(
        acquire_lock=AsyncMock(return_value=True),
        release_lock=AsyncMock(),
        backoff_remaining_seconds=AsyncMock(return_value=0),
        get_failure_count=AsyncMock(return_value=0),
        get_health=AsyncMock(return_value=None),
        record_health=AsyncMock(),
        record_failure=AsyncMock(return_value=1),
        clear_failures=AsyncMock(),
        format_error=str,
    )
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_ok_false_records_not_ok_without_backoff():
    c = _collabs()

    async def body():
        return TaskOutcome(row_count=0, status="gap: 2026-07-24", ok=False)

    import logging
    await run_guarded(
        job_id="j", lock_key="k", lock_ttl=60,
        log=logging.getLogger("t"), body=body, **c,
    )
    kwargs = c["record_health"].await_args.kwargs
    assert kwargs["ok"] is False
    assert "gap" in (kwargs.get("error") or "")
    c["record_failure"].assert_not_awaited()


@pytest.mark.asyncio
async def test_default_ok_true_unchanged():
    c = _collabs()

    async def body():
        return TaskOutcome(row_count=5)

    import logging
    await run_guarded(
        job_id="j", lock_key="k", lock_ttl=60,
        log=logging.getLogger("t"), body=body, **c,
    )
    assert c["record_health"].await_args.kwargs["ok"] is True
```

NOTE to implementer: read `_runner.py` first — the guard function's real
name and required params may differ slightly (e.g. `log_backoff_skip`);
adapt the test's call to the real signature, keeping the assertions.

- [ ] **Step 2: Run to verify it fails** — `ok` is not a `TaskOutcome` field yet: `TypeError: unexpected keyword argument 'ok'`.
- [ ] **Step 3: Implement** — add `ok: bool = True` to the dataclass; in the success path where the runner currently calls `record_health(job_id, ok=True, row_count=outcome.row_count, ...)`, pass `ok=outcome.ok`, and when `outcome.ok is False` route `outcome.status` into the `error` kwarg (the existing `status` plumbing already lands it there for ok=True tasks — verify and keep one mechanism). Do not touch the exception path.
- [ ] **Step 4: Run** the new test + `pytest tests/ -q -k "runner or _runner"` — all green.
- [ ] **Step 5: Lint + commit** — `git commit -m "feat(tasks): TaskOutcome.ok=False records an alarm without arming backoff"`

---

### Task 4: Chip outcome classifier (`not_yet_published` vs `gap`)

**Files:**
- Create: `backend/tasks/chip_outcome.py`
- Modify: `backend/tasks/ingest_institutional_tw.py` (`_do_run` tail + `run` body), `backend/tasks/ingest_margin_tw.py` (same shape)
- Test: `backend/tests/test_chip_outcome.py` (create)

**Interfaces:**
- Consumes: `TaskOutcome(..., ok=...)` from Task 3.
- Produces: `classify_chip_outcome(*, day_rows: dict[date, int], today: date, traded: set[date]) -> tuple[bool, str | None]` — pure. `day_rows` maps each fetched day to rows written; `traded` is the set of those days that have TW price bars in `ohlcv_daily` (the "it was a real trading day" witness). Returns `(ok, status)`.

Classification rules (spec Track 1, holiday-safe):

| Case | Result |
|---|---|
| any past day in `traded` with 0 rows | `(False, "gap: <days>")` — price landed, chips didn't |
| today with 0 rows (and no past gaps) | `(True, "not_yet_published: <today>")` |
| rows written, no gaps | `(True, None)` |
| nothing fetched at all (all skipped/weekend) | `(True, "idle: nothing due")` |

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_chip_outcome.py
"""Holiday-safe outcome classification for the chip ingest walk.

The naive rule "past day empty = gap" would fire on every holiday
(holidays stay pending forever by design). The witness is our own
price archive: if ohlcv_daily has TW bars for that day, the market
traded — chips missing is a real gap, not a holiday.
"""
from datetime import date

from tasks.chip_outcome import classify_chip_outcome

TODAY = date(2026, 7, 25)


def test_past_traded_day_empty_is_gap():
    ok, status = classify_chip_outcome(
        day_rows={date(2026, 7, 24): 0, TODAY: 0},
        today=TODAY,
        traded={date(2026, 7, 24)},
    )
    assert ok is False
    assert status is not None and status.startswith("gap: 2026-07-24")


def test_today_empty_is_not_yet_published():
    ok, status = classify_chip_outcome(
        day_rows={TODAY: 0}, today=TODAY, traded=set(),
    )
    assert ok is True
    assert status == "not_yet_published: 2026-07-25"


def test_past_holiday_empty_is_quiet():
    # 7-21 was a (hypothetical) holiday: no price bars → not a gap.
    ok, status = classify_chip_outcome(
        day_rows={date(2026, 7, 21): 0, date(2026, 7, 24): 1300},
        today=TODAY,
        traded={date(2026, 7, 24)},
    )
    assert ok is True
    assert status is None


def test_rows_written_clean():
    ok, status = classify_chip_outcome(
        day_rows={date(2026, 7, 24): 1300},
        today=TODAY,
        traded={date(2026, 7, 24)},
    )
    assert (ok, status) == (True, None)


def test_nothing_fetched_is_idle():
    ok, status = classify_chip_outcome(day_rows={}, today=TODAY, traded=set())
    assert (ok, status) == (True, "idle: nothing due")
```

- [ ] **Step 2: Run to verify it fails** — ModuleNotFoundError.
- [ ] **Step 3: Implement**

```python
# backend/tasks/chip_outcome.py
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
```

- [ ] **Step 4: Wire into both chip tasks.** In `ingest_institutional_tw._do_run`, collect `day_rows[day] = written` inside the walk loop (skipping failed days — they already have their own path), and after the loop query the witness set:

```python
    from sqlalchemy import select

    from models.ohlcv_daily import OhlcvDaily

    past_empty = [d for d, r in day_rows.items() if r == 0]
    traded: set[date] = set()
    if past_empty:
        async with AsyncSessionLocal() as db:
            hits = (await db.scalars(
                select(OhlcvDaily.ts).where(
                    OhlcvDaily.market == "TW",
                    OhlcvDaily.ts.in_(past_empty),
                    OhlcvDaily.symbol == "2330",   # one liquid witness row is enough
                )
            )).all()
        traded = {t.date() if hasattr(t, "date") else t for t in hits}
```

Change `_do_run` to return `tuple[int, bool, str | None]` (total, ok, status) using `classify_chip_outcome(day_rows=day_rows, today=date.today(), traded=traded)`, and the task body to `return TaskOutcome(row_count=total, ok=ok, status=status)`. Mirror the identical change in `ingest_margin_tw`. The existing all-days-failed re-raise stays untouched (transport failure keeps its backoff).
- [ ] **Step 5: Run** `pytest tests/test_chip_outcome.py tests/test_ingest_chip_metrics_tw_task.py -q` — the pre-existing chip task tests must stay green (they assert `record_health` kwargs; update ONLY if an assertion now sees `ok`/`status` values the new taxonomy legitimately changes, and say so in the report).
- [ ] **Step 6: Lint + commit** — `git commit -m "feat(ingest): chip walks classify not_yet_published vs gap with a price-archive witness"`

---

### Task 5: Evening re-probe schedules

**Files:**
- Modify: `backend/tasks/scheduler.py` (after the existing `ingest_institutional_tw` / `ingest_margin_tw` registrations, ~lines 304-325)
- Test: `backend/tests/test_scheduler_evening_chip.py` (create)

**Interfaces:**
- Consumes: the existing `run()` functions — idempotent by design (`pending_market_days` walk).
- Produces: job ids `ingest_institutional_tw_evening`, `ingest_margin_tw_evening`, both `CronTrigger(hour=13, minute=40, timezone="UTC")` (= 21:40 Taipei, after TWSE's evening ledger publication), `replace_existing=True, max_instances=1, coalesce=True`, invoking the SAME run functions (per-job Redis locks prevent overlap with the afternoon runs).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_scheduler_evening_chip.py
"""The 21:40 Taipei chip re-probe (spec Track 2).

The 17:10 run legitimately finds today's ledgers unpublished
(`not_yet_published`); this second run lands them the same evening so
the 04:00 discussion reads T-1 chips beside T-1 prices instead of T-2.
"""


def _job_ids_and_triggers():
    import tasks.scheduler as S
    registered = {}

    class _Spy:
        def add_job(self, func, trigger=None, id=None, **kw):
            registered[id] = trigger

        def start(self):  # pragma: no cover
            pass

    # The scheduler module exposes a build/registration function; the
    # implementer must locate the real entry point (grep "def " in
    # tasks/scheduler.py) and adapt — the assertion below is the contract.
    S.register_jobs(_Spy())          # ADAPT to the real registration API
    return registered


def test_evening_chip_jobs_registered_at_1340_utc():
    registered = _job_ids_and_triggers()
    for jid in ("ingest_institutional_tw_evening", "ingest_margin_tw_evening"):
        assert jid in registered, f"{jid} not registered"
        trig = str(registered[jid])
        assert "hour='13'" in trig and "minute='40'" in trig
```

NOTE to implementer: `tasks/scheduler.py` may not expose `register_jobs`
— find how the existing scheduler tests (`pytest tests/ -k scheduler`
lists them) construct/inspect the scheduler and use the same pattern.
The contract is only: both ids exist, cron is 13:40 UTC, and each `func`
is the same `run` the afternoon job uses.

- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** — two `scheduler.add_job` blocks copying the exact shape of the existing chip registrations (imports included), changing only `id=` (suffix `_evening`) and the trigger to `CronTrigger(hour=13, minute=40, timezone="UTC")`. Add a two-line comment: "Evening re-probe: picks up ledgers the 17:10 run classified not_yet_published; idempotent via pending_market_days; per-job lock prevents overlap."
- [ ] **Step 4: Run** the new test + `pytest tests/ -q -k scheduler`.
- [ ] **Step 5: Lint + commit** — `git commit -m "feat(scheduler): 21:40 Taipei chip re-probe — T-1 chips for the 04:00 discussion"`

---

### Task 6: TAIEX-TR ingest becomes append-only (compression prerequisite)

**Files:**
- Modify: `backend/tasks/ingest_taiex_tr_history.py`
- Test: `backend/tests/test_ingest_taiex_tr_history.py` (modify or create — check for an existing file first)

**Why:** health history shows this job upserts ~1,299 rows daily — the FULL total-return series back to the epoch — into `ohlcv_daily`. Once Task 7 compresses chunks older than 90 days, that daily full-history upsert would decompress/recompress old chunks every day. The fix is to clamp the fetch window to `max(archived ts) - 5 days` (small overlap heals revisions) so the job appends instead of rewriting history.

- [ ] **Step 1: Read the task file**, find where its date range is computed. Write a failing test pinning the clamp:

```python
# in backend/tests/test_ingest_taiex_tr_history.py
"""Append-only clamp: the TR ingest must not rewrite the full series
daily — compressed chunks (0099) would churn on every run."""
from datetime import date

from tasks.ingest_taiex_tr_history import clamp_fetch_start


def test_clamp_starts_just_before_newest_archived():
    assert clamp_fetch_start(
        newest_archived=date(2026, 7, 24), epoch=date(2021, 1, 1),
    ) == date(2026, 7, 19)     # newest - 5 days of healing overlap


def test_empty_archive_fetches_from_epoch():
    assert clamp_fetch_start(
        newest_archived=None, epoch=date(2021, 1, 1),
    ) == date(2021, 1, 1)
```

- [ ] **Step 2: Fail** (ImportError), **Step 3: implement**:

```python
# in backend/tasks/ingest_taiex_tr_history.py
_HEAL_OVERLAP_DAYS = 5


def clamp_fetch_start(*, newest_archived: date | None, epoch: date) -> date:
    """Append-only window: start just before the newest archived bar.

    The 5-day overlap re-upserts a handful of rows (idempotent) so a
    late upstream revision still heals, while the full-history rewrite
    — which would churn compressed chunks daily once 0099 lands — is
    gone."""
    if newest_archived is None:
        return epoch
    return newest_archived - timedelta(days=_HEAL_OVERLAP_DAYS)
```

then wire it where the task computes its fetch start (query `max(ts)` for `_TAIEX_TR` via the session the task already opens; the exact variable names come from reading the file — keep the change minimal and show it in the report).
- [ ] **Step 4: Run** the file's tests + `pytest tests/ -q -k taiex`.
- [ ] **Step 5: Lint + commit** — `git commit -m "fix(ingest): TAIEX-TR fetch is append-only — stop rewriting history daily"`

---

### Task 7: Migration 0099 — compression policies on the three chip/price hypertables

**Files:**
- Create: `backend/db/migrations/versions/0099_compress_chip_price_hypertables.py`
- Create: `backend/scripts/rehearse_timescale_migrations.sh`

**Interfaces:**
- Produces: compression enabled (`segmentby='market,symbol'`, `orderby='ts DESC'`) + `add_compression_policy(..., INTERVAL '90 days')` on `ohlcv_daily`, `tw_institutional_daily`, `tw_margin_daily`. Downgrade removes the policies and disables compression (decompressing any compressed chunks first).

- [ ] **Step 1: Write the migration**

```python
# backend/db/migrations/versions/0099_compress_chip_price_hypertables.py
"""Timescale compression on the chip/price hypertables (spec Track 3a).

Rows are NEVER deleted — compression only. 90-day threshold keeps every
chunk the daily ingest walks (10-day lookback) and the discussion reads
(30-day history windows) uncompressed; only cold history compresses.

Requires TimescaleDB community license (prod: `timescale`, 2.26.3 —
verified). Guarded so a plain-Postgres environment (unit-test SQLite is
irrelevant; alembic never runs there) fails loudly rather than half-applying.
"""
from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None

_TABLES = ("ohlcv_daily", "tw_institutional_daily", "tw_margin_daily")


def upgrade() -> None:
    for t in _TABLES:
        op.execute(
            f"ALTER TABLE {t} SET ("
            f"timescaledb.compress, "
            f"timescaledb.compress_segmentby = 'market,symbol', "
            f"timescaledb.compress_orderby = 'ts DESC')"
        )
        op.execute(
            f"SELECT add_compression_policy('{t}', INTERVAL '90 days')"
        )


def downgrade() -> None:
    for t in _TABLES:
        op.execute(
            f"SELECT remove_compression_policy('{t}', if_exists => true)"
        )
        op.execute(
            f"SELECT decompress_chunk(c, true) "
            f"FROM show_chunks('{t}') c"
        )
        op.execute(f"ALTER TABLE {t} SET (timescaledb.compress = false)")
```

- [ ] **Step 2: Write the rehearsal script**

```bash
#!/usr/bin/env bash
# backend/scripts/rehearse_timescale_migrations.sh
# Standing R6 rule, Timescale flavor: run the full migration chain
# against an ephemeral timescale container, then prove a compressed
# read returns identical rows. Run from backend/.
set -euo pipefail

IMG=timescale/timescaledb:2.26.3-pg16
NAME=rehearse-ts-$$
docker run -d --rm --name "$NAME" -e POSTGRES_PASSWORD=x -e POSTGRES_DB=rehearse \
  -p 55433:5432 "$IMG" >/dev/null
trap 'docker stop "$NAME" >/dev/null' EXIT
for i in $(seq 1 30); do
  docker exec "$NAME" pg_isready -U postgres -q && break; sleep 1
done

export DATABASE_URL="postgresql+asyncpg://postgres:x@127.0.0.1:55433/rehearse"
alembic upgrade head

# Compressed-read equivalence: seed 200 days of bars, force-compress,
# and diff a range read against the pre-compression answer.
docker exec -i "$NAME" psql -U postgres -d rehearse <<'SQL'
INSERT INTO ohlcv_daily (market, symbol, ts, open, high, low, close, volume)
SELECT 'TW', '2330', d::date, 100, 101, 99, 100.5, 1000
FROM generate_series(now() - interval '200 days', now(), interval '1 day') d
ON CONFLICT DO NOTHING;
CREATE TEMP TABLE before_c AS
  SELECT * FROM ohlcv_daily WHERE symbol='2330' ORDER BY ts;
SELECT compress_chunk(c, true) FROM show_chunks('ohlcv_daily', older_than => interval '90 days') c;
SELECT CASE WHEN count(*) = 0 THEN 'COMPRESSED-READ-OK'
       ELSE 'COMPRESSED-READ-MISMATCH' END
FROM (
  (SELECT market,symbol,ts,open,high,low,close,volume FROM ohlcv_daily WHERE symbol='2330'
   EXCEPT
   SELECT market,symbol,ts,open,high,low,close,volume FROM before_c)
  UNION ALL
  (SELECT market,symbol,ts,open,high,low,close,volume FROM before_c
   EXCEPT
   SELECT market,symbol,ts,open,high,low,close,volume FROM ohlcv_daily WHERE symbol='2330')
) diff;
SQL

alembic downgrade -1 && alembic upgrade head
echo "REHEARSAL COMPLETE"
```

NOTE to implementer: the alembic env may read the DB URL from a
different variable (check `backend/db/migrations/env.py` / `alembic.ini`)
— adapt the export line; and the seed INSERT's column list must match
the real `ohlcv_daily` schema (read the model first). Report the exact
output of the script including the `COMPRESSED-READ-OK` line.

- [ ] **Step 3: Run the rehearsal** — `bash scripts/rehearse_timescale_migrations.sh` from `backend/`. Expected: `COMPRESSED-READ-OK` and `REHEARSAL COMPLETE`. If the image cannot be pulled, report BLOCKED with the error — do not fake the rehearsal.
- [ ] **Step 4: Lint + commit** — `git commit -m "feat(db): 0099 — timescale compression on chip/price hypertables (90d)"`

---

### Task 8: Migration 0100 — `tw_stock_shareholding` becomes a compressed hypertable

**Files:**
- Create: `backend/db/migrations/versions/0100_shareholding_hypertable.py`
- Modify: `backend/scripts/rehearse_timescale_migrations.sh` (extend the seed/verify block)

**Facts (verified in recon):** PK is `(market, symbol, ts, bucket_id)` — contains the time column, so no PK surgery. 433 MB / 2.64 M rows / weekly cadence since 2025-09.

- [ ] **Step 1: Write the migration**

```python
# backend/db/migrations/versions/0100_shareholding_hypertable.py
"""tw_stock_shareholding → compressed hypertable (spec Track 3b).

433 MB and growing ~40 MB/month; weekly TDCC data compresses ~90% with
symbol segmenting. PK already contains `ts` so `create_hypertable` with
migrate_data is clean. Runs minutes of exclusive lock — acceptable in
the deploy window; only batch ingest touches this table.

NEVER deletes rows.
"""
from alembic import op

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "SELECT create_hypertable("
        "'tw_stock_shareholding', 'ts', "
        "chunk_time_interval => INTERVAL '1 month', "
        "migrate_data => true)"
    )
    op.execute(
        "ALTER TABLE tw_stock_shareholding SET ("
        "timescaledb.compress, "
        "timescaledb.compress_segmentby = 'market,symbol', "
        "timescaledb.compress_orderby = 'ts DESC, bucket_id')"
    )
    op.execute(
        "SELECT add_compression_policy("
        "'tw_stock_shareholding', INTERVAL '90 days')"
    )


def downgrade() -> None:
    # Hypertable conversion is one-way in place; reversing would mean a
    # full table rebuild. Removing the policy + decompressing restores
    # plain-table behaviour for reads/writes, which is all a rollback
    # needs.
    op.execute(
        "SELECT remove_compression_policy('tw_stock_shareholding', if_exists => true)"
    )
    op.execute(
        "SELECT decompress_chunk(c, true) "
        "FROM show_chunks('tw_stock_shareholding') c"
    )
```

- [ ] **Step 2: Extend the rehearsal script** with a shareholding block mirroring Task 7's (seed 200 days of weekly rows across 2 symbols with distinct bucket_ids — read the model for the full column list — force-compress older-than-90d, EXCEPT-diff before/after, expect `SHAREHOLDING-READ-OK`). Also verify the shareholding WRITER still works post-conversion: run one representative upsert statement (copy its ON CONFLICT form from the ingest code that writes this table — grep `tw_stock_shareholding` in `services/ingest/` / `tasks/`) against a compressed-era table and assert it succeeds.
- [ ] **Step 3: Run the full rehearsal** — both `COMPRESSED-READ-OK` and `SHAREHOLDING-READ-OK` must print.
- [ ] **Step 4: Lint + commit** — `git commit -m "feat(db): 0100 — shareholding hypertable + 90d compression"`

---

### Task 9: PR + verification (deploy is user-gated)

- [ ] **Step 1:** Full suites: `pytest tests/ -q` and `pytest finmind/tests/ -q` (separate processes, matching CI), whole-repo `--collect-only` clean, ruff on all touched files.
- [ ] **Step 2:** Push branch, open PR titled `feat(ingest,db): unified fetch-failure regime, evening chip re-probe, big-table compression`. Body: the taxonomy table, the buyback proving case, the 21:40 schedule, both migrations + rehearsal output pasted, the TAIEX-TR prerequisite, and the no-deletion rule. Note explicitly: **merge ≠ deploy; deploy waits for the experiment to finish and user approval.**
- [ ] **Step 3:** CI green → merge (standing authorization). **Do NOT deploy.**
- [ ] **Step 4 (post-deploy acceptance, after the user approves deploy):**
  - Next morning: freshness monitor should show institutional/margin at T-1 (same as ohlcv) on a normal weekday.
  - `SELECT count(*) FROM tw_stock_buyback;` > 0 within a day of the first buyback chunk retry.
  - `SELECT pg_size_pretty(pg_total_relation_size('tw_stock_shareholding'));` shrinks materially once the compression policy's background job has run (hours).
  - Dashboard: 17:10 chip runs show `not_yet_published: <today>` instead of bare ok-0.

---

## Self-review notes

- **Spec coverage:** taxonomy (T3+T4), 4xx fallback + buyback (T1+T2), removal of the commented-out `ingest_buyback_tw` scheduler block — **fold into T2 Step 3** (delete the comment block at `tasks/scheduler.py:443-447`, per spec "one mechanism, not two"); evening re-probe (T5); compression 3a (T7), 3b (T8), TAIEX-TR prerequisite (T6); no-deletion rule (global + both migrations); rehearsal rule (T7/T8 script); deploy gating (global + T9).
- **Known adaptation points are flagged inline** (scheduler registration API, alembic env URL var, ohlcv seed columns, `_runner` guard signature) rather than guessed — each tells the implementer exactly where to look.
- **Type consistency:** `classify_chip_outcome` returns `(bool, str | None)` consumed as `TaskOutcome(ok=..., status=...)`; `_fetch_with_fallback` returns the same `list[dict]` shape `ingest_chunk` already consumes.
