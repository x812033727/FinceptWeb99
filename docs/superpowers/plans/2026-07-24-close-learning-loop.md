# Close the daily-recommendation learning loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the daily-pick learning loop so knowledge compounds — schedule the never-run lesson promotion, make live discussions self-critique, and fuel the loop with a bounded historical sweep.

**Architecture:** The loop machinery already exists (sweep engine runs post-mortems, extracts lessons, refits calibration; live synthesis already applies the template calibration curve). This plan (a) extracts the sweep's post-mortem pass into a shared function and calls it from the live verify path, (b) adds a scheduled job that runs the existing `promote_eligible_lessons`, and (c) runs a bounded fuel sweep under the live owner, then measures.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy (async), APScheduler, pytest (`--asyncio-mode=auto`), Prometheus metrics.

## Global Constraints

- Backend timestamps: UTC, `DateTime(timezone=True)`. Copied verbatim from CLAUDE.md conventions.
- No comments explaining WHAT code does; only WHY when non-obvious. (CLAUDE.md)
- Run backend tests with the host venv: `/tmp/fincept-test-venv/bin/pytest tests/... --asyncio-mode=auto` (pytest is NOT in the backend container). CI runs `cd backend && pytest tests/ --asyncio-mode=auto`.
- Lint: `ruff check . --select E,W,F --ignore E501` (config in `backend/pyproject.toml`).
- Look-ahead safety (R2 `as_of` clamps) is unchanged by this plan — do not touch replay clamp logic.
- Owner scope for all fuel: the live auto-run owner `x812033727@gmail.com`. Lessons are owner-scoped via `shared_owner_ids`; the sweep MUST run under this owner or its outputs won't reach live.
- Fail-closed: a post-mortem error must never block or roll back the verdict write it hangs off.
- The only market with lessons is `TW`.
- Fincept99 standing authorization: self-merge each task's PR when CI is green.

---

## Phase 0 — Plumbing (cheap, low-risk, ships first)

### Task 1: Extract the post-mortem pass into a shared function

Today `_run_post_mortem_pass` lives inside `backtest_sweep_service.py`. Move it to `post_mortem_service.py` as a public `run_post_mortem_pass` so both the sweep and the live verify path call one implementation (DRY). Behavior must not change for the sweep.

**Files:**
- Modify: `backend/services/post_mortem_service.py` (add `run_post_mortem_pass`)
- Modify: `backend/services/backtest_sweep_service.py:351-439` (delete local `_run_post_mortem_pass`, import + call the shared one)
- Test: `backend/tests/test_post_mortem_pass.py` (new)

**Interfaces:**
- Produces: `async def run_post_mortem_pass(db: AsyncSession, discussion: Any, owner_id: UUID) -> None` in `post_mortem_service`. Same body as today's `_run_post_mortem_pass` (refresh → guard no-conclusion → build_post_mortem_message → win-skip lesson extraction OR full critique round + re-synthesize). Silent no-op when the discussion has no conclusion or the payload has no trading days.
- Consumes (unchanged): `discussion_service.{inject_user_message,run_round,synthesize_conclusion,extract_winning_thesis_lessons}`, `post_mortem_service.build_post_mortem_message`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_post_mortem_pass.py
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from services import post_mortem_service


@pytest.mark.asyncio
async def test_no_conclusion_is_a_silent_noop():
    db = AsyncMock()
    disc = SimpleNamespace(id=uuid4(), conclusion=None, market="TW")
    db.refresh = AsyncMock()
    with patch.object(post_mortem_service, "build_post_mortem_message", new=AsyncMock()) as build:
        await post_mortem_service.run_post_mortem_pass(db, disc, uuid4())
    build.assert_not_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/tmp/fincept-test-venv/bin/pytest tests/test_post_mortem_pass.py -v --asyncio-mode=auto`
Expected: FAIL with `AttributeError: module 'services.post_mortem_service' has no attribute 'run_post_mortem_pass'`

- [ ] **Step 3: Move the function**

In `backend/services/post_mortem_service.py`, add the function (copy the body of `_run_post_mortem_pass` from `backtest_sweep_service.py:351-439` verbatim, renamed and public):

```python
async def run_post_mortem_pass(db, discussion, owner_id) -> None:
    """Inject the post-mortem critique prompt, run one reflection round,
    and re-synthesize. Shared by the backtest sweep and the live verify
    path so both self-critique through one implementation."""
    from services import discussion_service

    await db.refresh(discussion)
    if not discussion.conclusion:
        return
    payload = await build_post_mortem_message(db, discussion)
    if not payload.trading_days:
        return
    if payload.verdict is not None and payload.verdict.status in ("win", "big_win"):
        try:
            from middleware.metrics import POST_MORTEM_SKIPPED_TOTAL
            POST_MORTEM_SKIPPED_TOTAL.labels(market=discussion.market).inc()
        except Exception:
            pass
        if payload.prompt_text:
            try:
                await discussion_service.extract_winning_thesis_lessons(
                    db, discussion, win_prompt_text=payload.prompt_text,
                    user_id=str(owner_id),
                )
            except Exception as exc:
                log.warning("post_mortem.win_lesson_extraction_failed",
                            extra={"discussion_id": str(discussion.id), "error": str(exc)})
        return
    if not payload.prompt_text:
        return
    await discussion_service.inject_user_message(db, discussion, content=payload.prompt_text)
    try:
        from middleware.metrics import POST_MORTEM_RAN_TOTAL
        POST_MORTEM_RAN_TOTAL.labels(market=discussion.market).inc()
    except Exception:
        pass
    async for _ev in discussion_service.run_round(db, discussion, user_id=str(owner_id), user_role="analyst"):
        pass
    await discussion_service.synthesize_conclusion(db, discussion, user_id=str(owner_id))
```

Then in `backtest_sweep_service.py`: delete the local `_run_post_mortem_pass` (lines 351-439) and change its call site (line ~327) from `await _run_post_mortem_pass(db, disc, sweep.owner_id)` to:

```python
from services.post_mortem_service import run_post_mortem_pass
await run_post_mortem_pass(db, disc, sweep.owner_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/tmp/fincept-test-venv/bin/pytest tests/test_post_mortem_pass.py tests/ -k "sweep or post_mortem" -v --asyncio-mode=auto`
Expected: PASS (new test green; existing sweep/post-mortem tests still green — behavior unchanged).

- [ ] **Step 5: Lint + commit**

```bash
cd backend && /tmp/fincept-test-venv/bin/ruff check services/post_mortem_service.py services/backtest_sweep_service.py tests/test_post_mortem_pass.py --select E,W,F --ignore E501
cd /opt/finceptweb99 && git add backend/services/post_mortem_service.py backend/services/backtest_sweep_service.py backend/tests/test_post_mortem_pass.py
git commit -m "refactor(post-mortem): share run_post_mortem_pass between sweep and live"
```

---

### Task 2: Wire the live post-mortem into the verify path

After a live discussion's verdict is written, run the shared post-mortem pass — but only on decided (non-abstain) outcomes, and fail-closed so a post-mortem error never disturbs the verdict.

**Files:**
- Modify: `backend/tasks/verify_discussion_outcome.py:559-570` (add the post-mortem call after the existing lesson-outcome hook)
- Test: `backend/tests/test_verify_live_post_mortem.py` (new)

**Interfaces:**
- Consumes: `post_mortem_service.run_post_mortem_pass(db, discussion, owner_id)` (Task 1).
- The verdict values that count as "decided" (a real pick was graded): `win`, `big_win`, `loss`, `big_loss`. `abstain`, `unverifiable`, and `None` are NOT decided — skip them (nothing to critique).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_verify_live_post_mortem.py
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from tasks import verify_discussion_outcome as V


def _disc(verdict):
    return SimpleNamespace(id=uuid4(), owner_id=uuid4(), verdict=verdict,
                           market="TW", conclusion={"x": 1})


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,should_run", [
    ("win", True), ("big_loss", True), ("loss", True),
    ("abstain", False), ("unverifiable", False), (None, False),
])
async def test_live_post_mortem_only_on_decided(verdict, should_run):
    db = AsyncMock()
    with patch.object(V, "run_post_mortem_pass", new=AsyncMock()) as pm:
        await V.maybe_run_live_post_mortem(db, _disc(verdict))
    assert pm.await_count == (1 if should_run else 0)


@pytest.mark.asyncio
async def test_live_post_mortem_is_fail_closed():
    db = AsyncMock()
    with patch.object(V, "run_post_mortem_pass", new=AsyncMock(side_effect=RuntimeError("boom"))):
        # Must not raise.
        await V.maybe_run_live_post_mortem(db, _disc("win"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/tmp/fincept-test-venv/bin/pytest tests/test_verify_live_post_mortem.py -v --asyncio-mode=auto`
Expected: FAIL with `AttributeError: ... has no attribute 'maybe_run_live_post_mortem'`

- [ ] **Step 3: Implement the helper and call it**

In `backend/tasks/verify_discussion_outcome.py`, add near the top-level helpers:

```python
from services.post_mortem_service import run_post_mortem_pass

DECIDED_VERDICTS = ("win", "big_win", "loss", "big_loss")


async def maybe_run_live_post_mortem(db, d) -> None:
    """Self-critique a graded live discussion. Only decided outcomes carry
    a real pick worth critiquing; abstain/unverifiable have nothing to
    reflect on. Fail-closed: a post-mortem error must never disturb the
    verdict already committed above."""
    if d.verdict not in DECIDED_VERDICTS:
        return
    try:
        await run_post_mortem_pass(db, d, d.owner_id)
    except Exception as exc:
        log.warning("verify_discussion_outcome.live_post_mortem_failed",
                    extra={"id": str(d.id), "error": str(exc)})
```

Then, right after the existing `record_lesson_outcome` block (after line 570), add:

```python
    await maybe_run_live_post_mortem(db, d)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/tmp/fincept-test-venv/bin/pytest tests/test_verify_live_post_mortem.py tests/ -k verify_discussion -v --asyncio-mode=auto`
Expected: PASS (all parametrized cases + fail-closed + existing verify tests).

- [ ] **Step 5: Lint + commit**

```bash
cd backend && /tmp/fincept-test-venv/bin/ruff check tasks/verify_discussion_outcome.py tests/test_verify_live_post_mortem.py --select E,W,F --ignore E501
cd /opt/finceptweb99 && git add backend/tasks/verify_discussion_outcome.py backend/tests/test_verify_live_post_mortem.py
git commit -m "feat(learning-loop): run post-mortem on decided live discussions"
```

---

### Task 3: Schedule the lesson promotion job

`promote_eligible_lessons` has never been scheduled. Add a weekly job so episodic lessons that earn their keep get promoted to `semantic` once fuel (Task 4/5) lifts hit rates past the threshold.

**Files:**
- Create: `backend/tasks/promote_lessons.py`
- Modify: `backend/tasks/scheduler.py` (register the job in `setup_jobs`)
- Test: `backend/tests/test_promote_lessons_task.py` (new)

**Interfaces:**
- Consumes: `lesson_tier_service.promote_eligible_lessons(db, *, market: str)` (returns `list[dict]` of promoted rows; commits internally).
- Produces: `async def run() -> None` in `tasks/promote_lessons.py` that opens a session and promotes for every market in `LESSON_MARKETS = ("TW",)`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_promote_lessons_task.py
from unittest.mock import AsyncMock, patch

import pytest

from tasks import promote_lessons


@pytest.mark.asyncio
async def test_run_promotes_every_market():
    with patch.object(promote_lessons, "promote_eligible_lessons",
                      new=AsyncMock(return_value=[])) as promote, \
         patch.object(promote_lessons, "AsyncSessionLocal") as sess:
        sess.return_value.__aenter__.return_value = AsyncMock()
        await promote_lessons.run()
    called_markets = [c.kwargs["market"] for c in promote.await_args_list]
    assert called_markets == list(promote_lessons.LESSON_MARKETS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/tmp/fincept-test-venv/bin/pytest tests/test_promote_lessons_task.py -v --asyncio-mode=auto`
Expected: FAIL with `ModuleNotFoundError: No module named 'tasks.promote_lessons'`

- [ ] **Step 3: Implement the task**

```python
# backend/tasks/promote_lessons.py
"""Weekly episodic→semantic lesson promotion. Wired into the scheduler;
`promote_eligible_lessons` is otherwise only reachable via an admin
endpoint, which is why the semantic tier stayed empty."""
import logging

from db.session import AsyncSessionLocal
from services.lesson_tier_service import promote_eligible_lessons

log = logging.getLogger(__name__)

LESSON_MARKETS = ("TW",)


async def run() -> None:
    async with AsyncSessionLocal() as db:
        for market in LESSON_MARKETS:
            try:
                promoted = await promote_eligible_lessons(db, market=market)
                if promoted:
                    log.info("promote_lessons.promoted",
                             extra={"market": market, "count": len(promoted)})
            except Exception as exc:
                log.warning("promote_lessons.failed",
                            extra={"market": market, "error": str(exc)})
```

In `backend/tasks/scheduler.py`, inside `setup_jobs()`, add (follow the existing `add_job` pattern):

```python
    from tasks.promote_lessons import run as run_promote_lessons
    scheduler.add_job(
        run_promote_lessons,
        trigger=CronTrigger(day_of_week="sun", hour=17, minute=0, timezone="UTC"),
        id="promote_lessons",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/tmp/fincept-test-venv/bin/pytest tests/test_promote_lessons_task.py -v --asyncio-mode=auto`
Expected: PASS

- [ ] **Step 5: Lint + commit**

```bash
cd backend && /tmp/fincept-test-venv/bin/ruff check tasks/promote_lessons.py tasks/scheduler.py tests/test_promote_lessons_task.py --select E,W,F --ignore E501
cd /opt/finceptweb99 && git add backend/tasks/promote_lessons.py backend/tasks/scheduler.py backend/tests/test_promote_lessons_task.py
git commit -m "feat(learning-loop): schedule weekly episodic->semantic promotion"
```

---

## Phase 1 — Fuel (operational; the only heavy-cost part)

> These tasks RUN the existing sweep engine; they add no product code. Run them from the repo root against the live stack. Deploy Phase 0 first (it must be live so promotion is scheduled and live post-mortems fire).

### Task 4: Dry-run the fuel pipeline (3 sessions)

Validate the pipeline end-to-end cheaply before spending on scale.

**Files:** none (operational).

- [ ] **Step 1: Confirm the live owner + usable date range**

Run (repo root):
```bash
/usr/local/bin/docker-compose exec -T backend python -c "print('owner=x812033727@gmail.com; usable range ~2026-04-20..present')"
```

- [ ] **Step 2: Create a 3-date sweep under the live owner via the existing service**

Use `backtest_sweep_service.create_sweep(...)` with `auto_post_mortem=True`, owner = the live owner's user id, over 3 stratified dates spanning the VIX bands (e.g. `2026-04-29, 2026-05-26, 2026-06-09`). Trigger the worker (`run_sweep_worker`). Follow the admin sweep endpoint / existing script path the operator already uses for sweeps.

- [ ] **Step 3: Verify the dry-run produced fuel**

```bash
/usr/local/bin/docker-compose exec -T backend python - <<'PY'
import asyncio
from sqlalchemy import text
from db.session import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        n=(await db.scalar(text("SELECT COUNT(*) FROM discussions WHERE as_of_date IS NOT NULL AND post_mortem_conclusion IS NOT NULL")))
        print("backtest discussions with post-mortem:", n)
asyncio.run(main())
PY
```
Expected: a small non-zero count (pipeline works).

- [ ] **Step 4: Record the dry-run result** in the sweep's status; do not proceed to Task 5 until the dry-run shows graded outcomes + post-mortems under the correct owner.

---

### Task 5: First bounded fuel batch (~20 sessions, budget-capped)

**Recommended starting parameters (adjust to your appetite):** ~20 stratified sessions across the usable range, `auto_post_mortem=True`, an explicit LLM-spend ceiling on the sweep. The sweep supports cancel/concurrency, so it is resumable — stop at the budget ceiling, never mid-write.

**Files:** none (operational).

- [ ] **Step 1: Launch the batch sweep** under the live owner over ~20 stratified dates with the budget ceiling set.
- [ ] **Step 2: Monitor** via the sweep status (existing `get_sweep` / admin UI) until complete or budget-halted.
- [ ] **Step 3: Verify fuel landed**

```bash
/usr/local/bin/docker-compose exec -T backend python - <<'PY'
import asyncio
from sqlalchemy import text
from db.session import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        graded=(await db.scalar(text("SELECT COUNT(*) FROM discussions WHERE as_of_date IS NOT NULL AND verdict IN ('win','big_win','loss','big_loss')")))
        sem=(await db.scalar(text("SELECT COUNT(*) FROM discussion_lessons WHERE tier='semantic'")))
        print("graded backtest outcomes:", graded, "| semantic lessons:", sem)
asyncio.run(main())
PY
```
Expected: graded count materially up; `semantic` may still be 0 until the weekly promotion job runs (Task 3) after hit rates rise — that is expected, verified in Phase 2.

---

## Phase 2 — Verify feedback + measure

### Task 6: Before/after measurement (acceptance gate)

**Files:**
- Create: `docs/superpowers/plans/2026-07-24-close-learning-loop-metrics.md` (record the numbers)

- [ ] **Step 1: Trigger promotion once manually** (don't wait a week for the cron) via the existing admin endpoint or:

```bash
/usr/local/bin/docker-compose exec -T backend python -c "import asyncio; from tasks.promote_lessons import run; asyncio.run(run())"
```

- [ ] **Step 2: Capture the acceptance metrics**

```bash
/usr/local/bin/docker-compose exec -T backend python - <<'PY'
import asyncio
from sqlalchemy import text
from db.session import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        sem=(await db.scalar(text("SELECT COUNT(*) FROM discussion_lessons WHERE tier='semantic'")))
        graded=(await db.scalar(text("SELECT COUNT(*) FROM discussions WHERE verdict IN ('win','big_win','loss','big_loss')")))
        curve=(await db.scalar(text("SELECT COUNT(*) FROM strategy_templates WHERE calibration_curve IS NOT NULL")))
        print(f"semantic_lessons={sem} graded_total={graded} templates_with_calibration={curve}")
asyncio.run(main())
PY
```

- [ ] **Step 3: Acceptance check**
  - PASS if `semantic_lessons > 0` (loop now distills durable knowledge) AND graded_total materially exceeds the pre-project ~13.
  - Record `templates_with_calibration` as progress toward the 30-sample calibration goal (NOT required to pass — accepted as long-horizon per the spec).
  - Write the before (semantic=0, graded≈13) and after numbers into the metrics file and commit.

- [ ] **Step 4: Commit the metrics record**

```bash
cd /opt/finceptweb99 && git add docs/superpowers/plans/2026-07-24-close-learning-loop-metrics.md
git commit -m "docs: record learning-loop close before/after metrics"
```

---

## Self-Review

**Spec coverage:**
- ① fuel → Tasks 4, 5. ② schedule promotion → Task 3. ②b criteria review → deferred by spec (noted, not a task). ③ calibration → accrual tracked in Task 6 (spec accepts no force-fit). ④ live post-mortem → Tasks 1, 2. ⑤ owner alignment → Global Constraints + Task 4 Step 1. Success criteria (semantic>0, graded up, feedback verified) → Task 6. All covered.

**Placeholder scan:** No TBD/TODO. Phase 1 tasks are operational-by-nature (they run an existing engine); parameters are given as concrete recommendations (3 dates, ~20 dates, named stratified sessions) with the budget ceiling flagged as the operator's appetite call, per spec's open question.

**Type consistency:** `run_post_mortem_pass(db, discussion, owner_id)` used identically in Tasks 1 and 2. `promote_eligible_lessons(db, *, market)` matches the real signature. `DECIDED_VERDICTS` defined once in Task 2 and reused. `LESSON_MARKETS` defined in Task 3.

**Out of scope (unchanged):** abstention reduction, fundamentals backfill, lowering `MIN_SAMPLES_FOR_FIT`, changing promotion criteria.
