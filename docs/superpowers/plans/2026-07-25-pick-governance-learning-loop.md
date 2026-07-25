# Pick Governance + Learning-Loop Hit Semantics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct abstention counts as a lesson hit (unblocking the never-fired semantic promotion), and the macro-veto downgrade ships with an audit script, a revert-guard alert, and a leakage watch — adoption itself is gated on the spec's pre-registered criteria applied to the experiment grades.

**Architecture:** One pure predicate (`qualifies_for_hit`) governs both the live gate (verify path) and a deterministic retroactive recount script. Governance lands as a pure `revert_trigger` helper wired into the existing `monitor_strategy_health` job plus an operator script that archives/applies/reverts the DB rules text. No schema changes.

**Tech Stack:** Python 3.12, SQLAlchemy async, pytest via host venv.

**Spec:** `docs/superpowers/specs/2026-07-25-pick-governance-learning-loop-design.md`

## Global Constraints

- Backend tests: `cd <worktree>/backend && /tmp/fincept-test-venv/bin/python -m pytest ...` (host venv).
- Lint: `/tmp/fincept-test-venv/bin/python -m ruff check --select E,W,F --ignore E501 <files>`.
- Never wall-clock in tests.
- Experiment rows (tagged `candidate_snapshot.experiment`) stay excluded from the loop — `is_experiment` gates are untouched.
- **The rules UPDATE (Part 1 adoption) is an operator action gated on the pre-registered criteria — no task in this plan applies it.** Task 5 only builds the tooling.
- Merge ≠ deploy. Deploy is user-gated (and currently blocked on the running experiment).

---

### Task 1: `qualifies_for_hit` predicate + internal gate

**Files:**
- Modify: `backend/services/lesson_tier_service.py` (`record_lesson_outcome`, gate at ~line 126)
- Test: `backend/tests/test_lesson_hit_semantics.py` (create)

**Interfaces:**
- Produces: `qualifies_for_hit(verdict: str | None, pool_avg_return_pct: float | None) -> bool` (module-level, pure): winning band → True; `"abstain"` with `pool_avg_return_pct` not None and `< 0` → True; everything else (including abstain over a rising or unmeasured pool) → False.
- `record_lesson_outcome` replaces its `is_winning_verdict` early-return with this predicate, reading `discussion.pool_performance.get("avg_return_pct")` (pool_performance is a JSON column; may be None).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_lesson_hit_semantics.py
"""Correct abstention counts as a hit (spec Part 2).

hit_count only-on-win is why zero lessons have EVER promoted across two
generations of the stack (max ratio 0.259 vs the 0.6 floor): a system
that correctly sits out falling tapes had its best behaviour scored as
failure. An abstention over a pool that then FELL is a vindicated call.
"""
import pytest

from services.lesson_tier_service import qualifies_for_hit


@pytest.mark.parametrize("verdict,pool,expected", [
    ("win", None, True),
    ("big_win", 2.0, True),
    ("abstain", -1.7, True),      # pool fell — the caution was right
    ("abstain", 0.0, False),      # flat pool: not vindicated
    ("abstain", 3.2, False),      # pool rose: a missed gain, not a hit
    ("abstain", None, False),     # unmeasured pool: no evidence, no hit
    ("loss", -5.0, False),
    ("big_loss", -8.0, False),
    ("unverifiable", -1.0, False),
    (None, -1.0, False),
])
def test_qualifies_for_hit(verdict, pool, expected):
    assert qualifies_for_hit(verdict, pool) is expected
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_lesson_hit_semantics.py -q` → ImportError.
- [ ] **Step 3: Implement**

```python
# in backend/services/lesson_tier_service.py (module level, near the top helpers)
def qualifies_for_hit(
    verdict: str | None, pool_avg_return_pct: float | None,
) -> bool:
    """Does this outcome vindicate the lessons the discussion cited?

    Wins obviously. An abstention whose candidate pool then FELL also
    does — the lessons that argued for caution were right, and scoring
    correct refusals as failure is why the promotion pipeline has never
    fired (max hit ratio 0.259 vs the 0.6 floor, across two stacks).
    An abstention over a rising or unmeasured pool earns nothing:
    usage still counts, so a lesson that talks the panel out of gains
    keeps sinking.
    """
    from services.outcome_classifier import is_winning_verdict

    if is_winning_verdict(verdict):
        return True
    return (
        verdict == "abstain"
        and pool_avg_return_pct is not None
        and pool_avg_return_pct < 0
    )
```

Then in `record_lesson_outcome`, replace:

```python
    from services.outcome_classifier import is_winning_verdict
    if not is_winning_verdict(discussion.verdict):
        return status_payload
```

with:

```python
    pool = discussion.pool_performance or {}
    pool_avg = pool.get("avg_return_pct") if isinstance(pool, dict) else None
    if not qualifies_for_hit(discussion.verdict, pool_avg):
        return status_payload
```

- [ ] **Step 4: Run** the new test + `pytest tests/ -q -k "lesson"` — all green.
- [ ] **Step 5: Lint + commit** — `git commit -m "feat(lessons): correct abstention over a falling pool counts as a hit"`

---

### Task 2: Verify-path call gate widens to abstain

**Files:**
- Modify: `backend/tasks/verify_discussion_outcome.py` (~line 592)
- Test: `backend/tests/test_verify_lesson_gate.py` (create)

**Interfaces:**
- Consumes: `qualifies_for_hit` indirectly — the call-site gate only needs to be *permissive enough*; the precise pool check lives inside `record_lesson_outcome` (single source of truth).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_verify_lesson_gate.py
"""The verify path must INVOKE record_lesson_outcome for abstain rows —
the precise falling-pool check lives inside the service (one gate, one
truth). Experiment rows stay excluded entirely (#264)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tasks.verify_discussion_outcome import should_record_lesson_outcome


@pytest.mark.parametrize("verdict,experiment,expected", [
    ("win", False, True),
    ("big_win", False, True),
    ("abstain", False, True),          # NEW: abstain reaches the service
    ("loss", False, False),
    ("unverifiable", False, False),
    ("win", True, False),              # experiment rows never feed the loop
    ("abstain", True, False),
])
def test_should_record_lesson_outcome(verdict, experiment, expected):
    d = SimpleNamespace(
        verdict=verdict,
        candidate_snapshot=(
            {"experiment": "rules_override"} if experiment else {}
        ),
    )
    assert should_record_lesson_outcome(verdict, d) is expected
```

- [ ] **Step 2: Run to verify it fails** — ImportError.
- [ ] **Step 3: Implement** — in `verify_discussion_outcome.py`, add near `is_experiment`:

```python
def should_record_lesson_outcome(verdict: str | None, d) -> bool:
    """Call-site gate for the lesson hit bump: winning bands and
    abstentions reach the service (which applies the precise
    falling-pool test); experiment rows never do (#264)."""
    from services.outcome_classifier import is_winning_verdict

    if is_experiment(d):
        return False
    return is_winning_verdict(verdict) or verdict == "abstain"
```

and replace the call-site condition `if is_winning_verdict(verdict) and not is_experiment(d):` with `if should_record_lesson_outcome(verdict, d):` (the comment above it updates to name both qualifying outcomes).
- [ ] **Step 4: Run** new test + `pytest tests/ -q -k "verify"` — green (the pre-existing verify suites must not regress).
- [ ] **Step 5: Lint + commit** — `git commit -m "feat(verify): abstain verdicts reach the lesson-outcome service"`

---

### Task 3: Deterministic retroactive recount script

**Files:**
- Create: `backend/scripts/recount_lesson_hits.py`
- Test: `backend/tests/test_recount_lesson_hits.py` (create — pure parts only)

**Interfaces:**
- Consumes: `qualifies_for_hit` (Task 1).
- Produces: CLI `python -m scripts.recount_lesson_hits [--apply]`. Default is dry-run. Recomputes each lesson's `hit_count` FROM SCRATCH over every verified, non-experiment discussion (win/big_win + qualifying abstains), prints per-tier summary and the **would-promote count** (`usage_count >= 5 and recomputed_hits / usage_count >= 0.6`) that the spec requires in the PR body. `--apply` overwrites `hit_count` in one transaction.

- [ ] **Step 1: Write the failing test for the pure aggregation**

```python
# backend/tests/test_recount_lesson_hits.py
"""Recount is a from-scratch aggregation, not an increment replay —
idempotent by construction, so running it twice cannot double-count."""
from scripts.recount_lesson_hits import aggregate_hits, would_promote


def test_aggregate_counts_once_per_qualifying_discussion():
    # (discussion qualifies?, lesson_ids cited)
    rows = [
        (True,  {1, 2}),
        (True,  {2}),
        (False, {1, 2, 3}),
    ]
    assert aggregate_hits(rows) == {1: 1, 2: 2}


def test_would_promote_applies_both_floors():
    # (usage, hits)
    assert would_promote(usage=10, hits=6) is True     # 0.6 exactly
    assert would_promote(usage=10, hits=5) is False    # ratio floor
    assert would_promote(usage=4, hits=4) is False     # usage floor
    assert would_promote(usage=0, hits=0) is False
```

- [ ] **Step 2: Run to verify it fails** — ModuleNotFoundError.
- [ ] **Step 3: Implement**

```python
# backend/scripts/recount_lesson_hits.py
"""Retroactive lesson-hit recount under the corrected semantics.

Spec Part 2 requires the PR to answer: how many of the existing
episodic lessons WOULD cross the promotion floor once correct
abstentions count? A zero here means the semantics change alone is
insufficient and the threshold discussion starts now, not in two weeks.

From-scratch recount (idempotent), dry-run by default; --apply
overwrites hit_count. Never promotes — the Sunday job owns that.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from sqlalchemy import select, update

from db.session import AsyncSessionLocal
from models.discussion import Discussion
from models.discussion_lesson import DiscussionLesson
from models.discussion_round_context import DiscussionRoundContext
from services.lesson_tier_service import (
    PROMOTE_MIN_USAGE,
    _extract_lesson_id,
    qualifies_for_hit,
)
from tasks.verify_discussion_outcome import is_experiment

PROMOTE_MIN_HIT_RATE = 0.6


def aggregate_hits(rows: list[tuple[bool, set[int]]]) -> dict[int, int]:
    """One hit per qualifying discussion per cited lesson."""
    counts: Counter[int] = Counter()
    for qualifies, lesson_ids in rows:
        if not qualifies:
            continue
        for lid in lesson_ids:
            counts[lid] += 1
    return dict(counts)


def would_promote(*, usage: int, hits: int) -> bool:
    return (
        usage >= PROMOTE_MIN_USAGE
        and usage > 0
        and hits / usage >= PROMOTE_MIN_HIT_RATE
    )


def _lesson_ids_from_snapshots(snapshots) -> set[int]:
    ids: set[int] = set()
    for snap in snapshots:
        if not isinstance(snap, dict):
            continue
        recent = snap.get("recent_lessons") or {}
        if not isinstance(recent, dict):
            continue
        for entry in recent.get("market") or []:
            lid = _extract_lesson_id(entry)
            if lid is not None:
                ids.add(lid)
        per_symbol = recent.get("per_symbol") or {}
        if isinstance(per_symbol, dict):
            for entries in per_symbol.values():
                for entry in entries or []:
                    lid = _extract_lesson_id(entry)
                    if lid is not None:
                        ids.add(lid)
    return ids


async def _collect(db) -> list[tuple[bool, set[int]]]:
    discussions = (await db.scalars(
        select(Discussion).where(Discussion.verdict.is_not(None))
    )).all()
    rows: list[tuple[bool, set[int]]] = []
    for d in discussions:
        if is_experiment(d):
            continue
        pool = d.pool_performance or {}
        pool_avg = pool.get("avg_return_pct") if isinstance(pool, dict) else None
        qualifies = qualifies_for_hit(d.verdict, pool_avg)
        snaps = (await db.scalars(
            select(DiscussionRoundContext.context).where(
                DiscussionRoundContext.discussion_id == d.id
            )
        )).all()
        rows.append((qualifies, _lesson_ids_from_snapshots(snaps)))
    return rows


async def main(apply: bool) -> None:
    async with AsyncSessionLocal() as db:
        rows = await _collect(db)
        hits = aggregate_hits(rows)
        lessons = (await db.scalars(select(DiscussionLesson))).all()
        promotable = 0
        changed = 0
        for lesson in lessons:
            new_hits = hits.get(lesson.id, 0)
            if new_hits != (lesson.hit_count or 0):
                changed += 1
            if would_promote(usage=lesson.usage_count or 0, hits=new_hits):
                promotable += 1
        print(f"discussions considered: {len(rows)} "
              f"(qualifying: {sum(1 for q, _ in rows if q)})")
        print(f"lessons: {len(lessons)}, hit_count changes: {changed}")
        print(f"WOULD-PROMOTE under new semantics: {promotable}")
        if not apply:
            print("dry-run — pass --apply to write")
            return
        for lesson in lessons:
            await db.execute(
                update(DiscussionLesson)
                .where(DiscussionLesson.id == lesson.id)
                .values(hit_count=hits.get(lesson.id, 0))
                .execution_options(synchronize_session=False)
            )
        await db.commit()
        print("applied")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.apply))
```

NOTE to implementer: verify the model import paths (`models.discussion_lesson`,
`models.discussion_round_context`) and the constant name
`PROMOTE_MIN_HIT_RATE` in `lesson_tier_service` (import it if exported rather
than redefining). If `_extract_lesson_id` is private, importing it is
acceptable here (same package family) — note it in the report.
- [ ] **Step 4: Run** pure tests; then a DRY-RUN against production DB via
`docker-compose exec -T backend python -m scripts.recount_lesson_hits` —
requires the branch's code in the container, so if not deployed, run the
dry-run logic read-only via a host-side check or defer with a note that the
dry-run executes at acceptance time. Never pass --apply in this task.
- [ ] **Step 5: Lint + commit** — `git commit -m "feat(scripts): deterministic lesson-hit recount with would-promote report"`

---

### Task 4: Revert-guard + leakage watch in the strategy health monitor

**Files:**
- Create: `backend/services/veto_guard.py`
- Modify: `backend/tasks/monitor_strategy_health.py` (inside `run_health_monitor`, alongside its existing checks)
- Test: `backend/tests/test_veto_guard.py` (create)

**Interfaces:**
- Produces: `revert_trigger(verdicts_newest_first: list[str]) -> str | None` — fires on 3 consecutive decided losses (loss/big_loss, ignoring non-decided verdicts) or ≥2 big_losses within the newest 10 decided; returns the alert text (naming the revert instruction) or None. And `abstention_leakage(current_rate: float, baseline_rate: float) -> bool` — True when baseline − current > 0.20.
- The monitor queries live (as_of_date IS NULL) price_signal verdicts newest-first for the guard, and per-strategy abstention rates (last 14d vs the 30d before that) for chip_quality/general; findings append to the job's not-ok health error text.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_veto_guard.py
"""Post-adoption tripwires for the macro-veto downgrade (spec Part 1).

Revert stays a HUMAN action — the guard's job is to say, loudly and
with instructions, when the pre-registered revert condition is met."""
from services.veto_guard import abstention_leakage, revert_trigger


def test_three_consecutive_losses_fires():
    msg = revert_trigger(["loss", "big_loss", "loss", "win", "win"])
    assert msg is not None and "revert" in msg.lower()


def test_wins_break_the_streak():
    assert revert_trigger(["loss", "win", "loss", "loss"]) is None


def test_two_big_losses_in_rolling_ten_fires():
    verdicts = ["big_loss"] + ["win"] * 5 + ["big_loss"] + ["win"] * 3
    assert revert_trigger(verdicts) is not None


def test_two_big_losses_spread_beyond_ten_is_quiet():
    verdicts = ["big_loss"] + ["win"] * 10 + ["big_loss"]
    assert revert_trigger(verdicts) is None


def test_abstains_are_ignored_by_the_streak():
    assert revert_trigger(["loss", "abstain", "loss", "abstain", "loss"]) is not None


def test_leakage_threshold():
    assert abstention_leakage(current_rate=0.30, baseline_rate=0.55) is True
    assert abstention_leakage(current_rate=0.40, baseline_rate=0.55) is False
```

- [ ] **Step 2: Run to verify fail** — ModuleNotFoundError.
- [ ] **Step 3: Implement**

```python
# backend/services/veto_guard.py
"""Tripwires for the macro-veto downgrade (spec Part 1 governance).

Pure functions — the monitor supplies data, these supply judgment.
Revert itself is deliberately human: automation that rewrites trading
rules unattended is a bigger risk than the alert-to-action delay.
"""
from __future__ import annotations

_DECIDED = ("win", "big_win", "loss", "big_loss")
_REVERT_INSTRUCTION = (
    "REVERT CONDITION MET for price_signal veto downgrade — run "
    "`python -m scripts.apply_veto_downgrade --revert` (one DB UPDATE; "
    "archived original text)."
)


def revert_trigger(verdicts_newest_first: list[str]) -> str | None:
    decided = [v for v in verdicts_newest_first if v in _DECIDED]
    streak = 0
    for v in decided:
        if v in ("loss", "big_loss"):
            streak += 1
            if streak >= 3:
                return f"3 consecutive decided losses. {_REVERT_INSTRUCTION}"
        else:
            break
    if sum(1 for v in decided[:10] if v == "big_loss") >= 2:
        return f"2 big_losses within the last 10 decided. {_REVERT_INSTRUCTION}"
    return None


def abstention_leakage(*, current_rate: float, baseline_rate: float) -> bool:
    """Prompt-scoped clause bleeding into other strategies shows up as
    their abstention rate collapsing. >20pp drop = investigate."""
    return (baseline_rate - current_rate) > 0.20
```

Wire into `run_health_monitor`: query the newest 15 live price_signal verdicts
(`Discussion.auto_run_strategy == 'price_signal'`, `as_of_date IS NULL`,
`auto_run IS TRUE`, `verdict IS NOT NULL`, order `created_at desc`), call
`revert_trigger`; for chip_quality/general compute abstain-share over the last
14 days vs the 30 days before, call `abstention_leakage` (skip when either
window has < 5 verdicts — noise floor). Any finding appends to the monitor's
error text and forces `ok=False`. Follow the surrounding code's query style.
- [ ] **Step 4: Run** new tests + `pytest tests/ -q -k "monitor_strategy or veto_guard"` — green.
- [ ] **Step 5: Lint + commit** — `git commit -m "feat(monitoring): veto-downgrade revert guard + leakage watch"`

---

### Task 5: Operator script — archive / apply / revert the rules clause

**Files:**
- Create: `backend/scripts/apply_veto_downgrade.py`
- Test: `backend/tests/test_apply_veto_downgrade.py` (pure parts)

**Interfaces:**
- Produces: CLI with `--show` (default: print current rules + whether the clause is present), `--apply` (append the clause + print the archived original), `--revert` (strip the clause exactly). The clause text is the spec's, embedded as a module constant `VETO_DOWNGRADE_CLAUSE`. Idempotent: `--apply` when present and `--revert` when absent are no-ops that say so.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_apply_veto_downgrade.py
from scripts.apply_veto_downgrade import (
    VETO_DOWNGRADE_CLAUSE,
    apply_clause,
    revert_clause,
)


def test_apply_appends_once():
    base = "原有規則。"
    once = apply_clause(base)
    assert VETO_DOWNGRADE_CLAUSE in once
    assert apply_clause(once) == once          # idempotent


def test_revert_restores_exactly():
    base = "原有規則。"
    assert revert_clause(apply_clause(base)) == base
    assert revert_clause(base) == base          # no-op when absent


def test_clause_is_strategy_scoped_in_wording():
    assert "量價訊號" in VETO_DOWNGRADE_CLAUSE
    assert "部位上限減半" in VETO_DOWNGRADE_CLAUSE
```

- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement**

```python
# backend/scripts/apply_veto_downgrade.py
"""Operator tool for the macro-veto downgrade clause (spec Part 1).

Adoption is GATED on the spec's pre-registered criteria — running
--apply is the human act of adoption, after the experiment grades land
in the criteria table. The original text is printed AND written to
docs/rules-archive/ before any change, so revert is always possible
even without this script.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from db.session import AsyncSessionLocal
from models.discussion_auto_run_config import DiscussionAutoRunConfig

VETO_DOWNGRADE_CLAUSE = (
    "\n\n【僅適用於量價訊號策略場次】總經逆風(外資台指期淨空、三大法人連續"
    "賣超、台VIX 偏高等系統性風險)不得作為否決個股的唯一理由。當候選同時"
    "滿足技術面與籌碼面進場條件時仍應給出推薦,但總經逆風時必須:(1) 建議"
    "部位上限減半;(2) 停損位收緊並明確標出;(3) 在 risks 首條標注「總經"
    "逆風環境」。僅當個股本身不符技術或籌碼條件、或風報比不足時才棄權。"
)


def apply_clause(rules: str) -> str:
    if VETO_DOWNGRADE_CLAUSE in rules:
        return rules
    return rules + VETO_DOWNGRADE_CLAUSE


def revert_clause(rules: str) -> str:
    return rules.replace(VETO_DOWNGRADE_CLAUSE, "")


async def main(mode: str) -> None:
    async with AsyncSessionLocal() as db:
        cfgs = (await db.scalars(select(DiscussionAutoRunConfig))).all()
        for cfg in cfgs:
            rules = cfg.rules or ""
            present = VETO_DOWNGRADE_CLAUSE in rules
            print(f"config user={cfg.user_id} clause_present={present}")
            if mode == "show":
                continue
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            archive = Path("docs/rules-archive")
            archive.mkdir(parents=True, exist_ok=True)
            (archive / f"rules-{cfg.user_id}-{stamp}.txt").write_text(rules)
            print(f"--- archived original ({len(rules)} chars) ---")
            new_rules = (
                apply_clause(rules) if mode == "apply" else revert_clause(rules)
            )
            if new_rules == rules:
                print("no-op (already in desired state)")
                continue
            cfg.rules = new_rules
            await db.commit()
            print(f"{mode} done for user={cfg.user_id}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true")
    g.add_argument("--revert", action="store_true")
    args = ap.parse_args()
    mode = "apply" if args.apply else "revert" if args.revert else "show"
    asyncio.run(main(mode))
```

NOTE to implementer: the archive path resolves relative to the container CWD —
verify where `python -m scripts...` runs from inside the backend container and
anchor the path so the archive lands in the repo's bind-mounted or otherwise
retrievable location; if no writable bind mount exists, print the full original
text to stdout as the primary archive (the operator captures it) and say so in
the report.
- [ ] **Step 4: Run** the pure tests + lint.
- [ ] **Step 5: Commit** — `git commit -m "feat(scripts): veto-downgrade apply/revert with archived originals"`

---

### Task 6: PR + operator sequence

- [ ] **Step 1:** Full backend + finmind suites (separate processes), `--collect-only` clean, ruff.
- [ ] **Step 2:** PR titled `feat(lessons,governance): abstention hits + veto-downgrade tooling`. Body must include: the semantics change, the pre-registered criteria table copied from the spec, the revert-guard thresholds, and an explicit statement that **--apply has not been run** — adoption happens only after the experiment grades land in the criteria table, as an operator action recorded in a PR comment.
- [ ] **Step 3:** CI green → merge (standing authorization). Do NOT deploy; do NOT run --apply.
- [ ] **Step 4 (operator, post-merge, post-deploy):** run the recount dry-run in the container, paste the WOULD-PROMOTE number into the PR; grade the experiment; fill the criteria table; if adoption row → run `--apply` and record the archived text reference; run `--apply` for the recount (`recount_lesson_hits --apply`) once its dry-run output is sane.

---

## Self-review notes

- **Spec coverage:** Part 2 gate (T1+T2), retroactive would-promote (T3), revert guard + leakage watch (T4), audit/apply/revert tooling (T5), pre-registered criteria + operator gating (T6 + global constraint). Thresholds unchanged (explicitly no task). Experiment-row exclusion untouched (asserted in T2's tests).
- **Type consistency:** `qualifies_for_hit(verdict, pool_avg)` used identically in T1 (service) and T3 (script); `revert_trigger` returns `str | None` consumed as alert text.
- **Adaptation points flagged inline:** model import paths (T3), archive path anchoring (T5), monitor query style (T4).
