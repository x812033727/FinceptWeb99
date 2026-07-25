# Confidence Tiering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-time recommend/watch tier over daily picks — server-computed, threshold-calibrated against the 19 graded experiment picks, surfaced on the public page, notifications, and scoreboard. Zero change to picking behaviour.

**Architecture:** One pure function (`services/daily_pick_tier.py`) is the single tier truth; the public payload carries a server-computed `tier`; the notification and scoreboard consume the same function. A read-only calibration script sweeps `T_consensus` over graded picks; its output sets the shipped constants.

**Tech Stack:** Python 3.12 backend (host-venv pytest), React 18 + TS frontend (vitest), no DB changes.

**Spec:** `docs/superpowers/specs/2026-07-25-confidence-tiering-design.md`

## Global Constraints

- Backend tests: `cd <worktree>/backend && /tmp/fincept-test-venv/bin/python -m pytest ...`; frontend: `cd <worktree>/frontend && npx vitest run ...` + `npx tsc --noEmit`.
- Lint: ruff `--select E,W,F --ignore E501`; frontend `npm run lint` if defined.
- No wall-clock in tests. No schema changes. **No change to picking/abstention/rules/verdicts/guard.**
- The 20-decided evaluation window is untouched — tiering is presentation.
- Implementation starts AFTER the 2026-07-28 maiden-run acceptance. Merge ≠ deploy; deploy is user-gated.
- Feature branch off `main`; one PR; CI green → standing self-merge.

---

### Task 1: The tier function

**Files:**
- Create: `backend/services/daily_pick_tier.py`
- Test: `backend/tests/test_daily_pick_tier.py`

**Interfaces:**
- Produces: `tier_for(conclusion: dict | None) -> str | None` — `"recommend"` / `"watch"` for pick-bearing conclusions, `None` for abstentions/empty/malformed. Constants `T_CONSENSUS = 0.85`, `T_HALLUC = 2` (provisional — Task 2's calibration output supersedes them in the same PR).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_daily_pick_tier.py
"""Read-time confidence tier. Presentation only: the 20-decided rule
guards picking, and this function never touches picking."""
import pytest

from services.daily_pick_tier import T_CONSENSUS, T_HALLUC, tier_for


def _conclusion(consensus=0.9, warnings=0, symbols=("2330",)):
    return {
        "recommended_symbols": list(symbols),
        "consensus_score": consensus,
        "quality_signals": {
            "hallucination_warnings": [{"round": 1}] * warnings,
        },
    }


def test_high_consensus_low_warnings_is_recommend():
    assert tier_for(_conclusion(consensus=0.9, warnings=0)) == "recommend"


def test_threshold_boundary_is_inclusive():
    assert tier_for(_conclusion(consensus=T_CONSENSUS, warnings=T_HALLUC)) == "recommend"


def test_low_consensus_is_watch():
    assert tier_for(_conclusion(consensus=T_CONSENSUS - 0.01)) == "watch"


def test_warning_heavy_is_watch():
    assert tier_for(_conclusion(warnings=T_HALLUC + 1)) == "watch"


def test_abstention_has_no_tier():
    assert tier_for({"recommended_symbols": [], "abstained": True}) is None


@pytest.mark.parametrize("broken", [
    None,
    {},
    {"recommended_symbols": ["2330"]},                     # no consensus_score
    {"recommended_symbols": ["2330"], "consensus_score": "high"},  # wrong type
    {"recommended_symbols": ["2330"], "consensus_score": 0.9,
     "quality_signals": "corrupt"},
])
def test_malformed_never_crashes_and_never_recommends(broken):
    assert tier_for(broken) in (None, "watch")
```

- [ ] **Step 2: Run to verify it fails** — ModuleNotFoundError.
- [ ] **Step 3: Implement**

```python
# backend/services/daily_pick_tier.py
"""Read-time confidence tier for daily picks (spec: confidence-tiering).

Pure and derived — no storage, no behaviour change, retroactively
consistent for every historical pick. Missing/malformed inputs degrade
to "watch" (or None when there is no pick at all): the recommend tier
must be EARNED by clean signals, never granted by absent ones.

Thresholds are calibrated against graded experiment picks
(scripts/calibrate_pick_tier.py); treat the constants as data, not
opinion — recalibrations ship with the sweep output attached.
"""
from __future__ import annotations

from typing import Any

T_CONSENSUS = 0.85
T_HALLUC = 2


def tier_for(conclusion: dict[str, Any] | None) -> str | None:
    if not isinstance(conclusion, dict):
        return None
    symbols = conclusion.get("recommended_symbols") or []
    if not symbols:
        return None

    consensus = conclusion.get("consensus_score")
    if not isinstance(consensus, (int, float)) or consensus < T_CONSENSUS:
        return "watch"

    quality = conclusion.get("quality_signals")
    warnings = (
        quality.get("hallucination_warnings")
        if isinstance(quality, dict) else None
    )
    n_warnings = len(warnings) if isinstance(warnings, list) else T_HALLUC + 1
    if n_warnings > T_HALLUC:
        return "watch"
    return "recommend"
```

NOTE to implementer: the spec's third MVP criterion (technical+chip
confluence) collapsed to "the pick came from a candidate snapshot" during
design — every auto-run pick does, so it adds nothing; the two live criteria
are consensus and hallucination hygiene. Say so in your report (the spec's
Calibration section governs anyway).

- [ ] **Step 4: Run** the tests — 10 passed. **Step 5: ruff + commit** — `feat(tier): read-time confidence tier for daily picks`

---

### Task 2: Calibration sweep script

**Files:**
- Create: `backend/scripts/calibrate_pick_tier.py`
- Test: `backend/tests/test_calibrate_pick_tier.py` (pure sweep only)

**Interfaces:**
- Consumes: `tier_for`-style logic parameterized by threshold: the sweep evaluates candidate `T_CONSENSUS` values, holding `T_HALLUC` fixed.
- Produces: pure `sweep(picks: list[dict], thresholds: list[float]) -> list[dict]` where each pick dict is `{"consensus": float, "warnings": int, "excess_pp": float, "win": bool}` and each result row is `{"threshold", "n_recommend", "recommend_win_rate", "recommend_mean_excess", "n_watch", "watch_win_rate"}`; CLI `python -m scripts.calibrate_pick_tier` (read-only, prints the sweep over experiment rows: `auto_run_sequence >= 900`, joined to their graded D5-vs-TAIEX outcomes computed the same way `scratchpad` grading did — reuse `_TAIEX_TR` benchmark reads from `daily_scoreboard_service`'s helpers where importable).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_calibrate_pick_tier.py
"""Threshold sweep is pure math over graded picks — the operator run
against real data lands in the PR body, not in tests."""
from scripts.calibrate_pick_tier import sweep


PICKS = [
    {"consensus": 0.95, "warnings": 0, "excess_pp": 10.0, "win": True},
    {"consensus": 0.90, "warnings": 1, "excess_pp": 5.0,  "win": True},
    {"consensus": 0.80, "warnings": 0, "excess_pp": -3.0, "win": False},
    {"consensus": 0.70, "warnings": 4, "excess_pp": 2.0,  "win": True},
]


def test_sweep_partitions_and_rates():
    rows = sweep(PICKS, thresholds=[0.85])
    row = rows[0]
    assert row["threshold"] == 0.85
    assert row["n_recommend"] == 2            # 0.95 and 0.90 (warnings ok)
    assert row["recommend_win_rate"] == 1.0
    assert row["recommend_mean_excess"] == 7.5
    assert row["n_watch"] == 2
    assert row["watch_win_rate"] == 0.5


def test_sweep_high_threshold_shrinks_tier():
    rows = sweep(PICKS, thresholds=[0.99])
    assert rows[0]["n_recommend"] == 0
    assert rows[0]["recommend_win_rate"] is None


def test_warning_gate_applies_inside_sweep():
    picks = [{"consensus": 0.95, "warnings": 9, "excess_pp": 8.0, "win": True}]
    rows = sweep(picks, thresholds=[0.85])
    assert rows[0]["n_recommend"] == 0
```

- [ ] **Step 2: fail (ModuleNotFoundError)**. **Step 3: Implement** — module with `T_HALLUC` imported from `services.daily_pick_tier`, `sweep()` as specified (win_rate `None` when a tier is empty; means rounded to 2dp), and an async `main()` that: loads experiment discussions (`auto_run_sequence >= 900`, `verdict`/conclusion present, non-abstained), computes each pick-session's mean D5 excess vs `_TAIEX_TR` (window logic mirrors `daily_scoreboard_service._benchmark_return_pct` — import it rather than re-implementing if importable; note which you did), prints the sweep for thresholds `[0.75, 0.80, 0.85, 0.90, 0.95]` plus the spec's selection rule verdict (win_rate ≥ 0.8 and coverage ≥ ⅓, ties → larger tier). Import-safety: DB imports inside `main()` (established pattern; subprocess purity test like `test_recount_lesson_hits.py`'s).
- [ ] **Step 4: run tests + subprocess purity test. Step 5: ruff + commit** — `feat(scripts): tier-threshold calibration sweep over graded picks`

---

### Task 3: Public payload carries the tier

**Files:**
- Modify: `backend/api/public_daily.py` (`PublicDailyResult` + the serialization site(s) that build results/strategies/days)
- Test: `backend/tests/test_public_daily.py` (append)

**Interfaces:**
- Produces: `PublicDailyResult.tier: str | None = None`, computed server-side via `tier_for(row.conclusion)` at every site that constructs a `PublicDailyResult` (find them all — main result, strategies dict, days history). Single source of truth: the frontend must never re-derive.

- [ ] **Step 1: Failing test** (append; follow the file's existing fixture style — `discussion(owner.id, ...)` helper):

```python
@pytest.mark.asyncio
async def test_public_daily_carries_server_computed_tier(client, db_session, monkeypatch):
    owner = User(id=uuid.uuid4(), email="tier-pub@example.com", hashed_password="x",
                 role=UserRole.viewer, is_active=True)
    db_session.add(owner)
    await db_session.flush()
    strong = {
        "recommended_symbols": ["2330"], "reasoning": "理由", "risks": [],
        "time_horizon": "short_term", "consensus_score": 0.95,
        "quality_signals": {"hallucination_warnings": []},
    }
    weak = dict(strong, consensus_score=0.5, recommended_symbols=["1101"])
    d1 = discussion(owner.id, created_at=datetime(2026, 7, 14, tzinfo=UTC), conclusion=strong)
    d1.auto_run_strategy = "price_signal"; d1.auto_run_date = datetime(2026, 7, 14, tzinfo=UTC).date()
    d2 = discussion(owner.id, created_at=datetime(2026, 7, 14, 1, tzinfo=UTC), conclusion=weak)
    d2.auto_run_strategy = "general"; d2.auto_run_date = datetime(2026, 7, 14, tzinfo=UTC).date()
    db_session.add_all([d1, d2])
    await db_session.commit()
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "tier-pub@example.com")

    body = (await client.get("/api/public/daily")).json()
    tiers = {runs[0]["strategy"]: runs[0]["tier"]
             for runs in body["strategies"].values() for runs in [runs]}
    flat = {r["strategy"]: r["tier"] for runs in body["strategies"].values() for r in runs}
    assert flat["price_signal"] == "recommend"
    assert flat["general"] == "watch"
```

NOTE: adapt the assertion to the payload's real shape after reading the serializer — the contract is: every serialized run carries `tier`, values as computed by `tier_for`.
- [ ] **Step 2: fail (KeyError 'tier')**. **Step 3: implement** — add the field + `tier=tier_for(...)` at each construction site (grep `PublicDailyResult(`). **Step 4: whole `test_public_daily.py` green. Step 5: ruff + commit** — `feat(public): server-computed pick tier on the daily payload`

---

### Task 4: Notification names the recommend tier

**Files:**
- Modify: `backend/tasks/auto_run_discussion.py` (`_run_for_user` collects tiers; `_notify_daily_ready` message)
- Test: `backend/tests/test_notify_daily_tiers.py` (create)

**Interfaces:**
- Consumes: `tier_for`.
- Produces: `_notify_daily_ready(user_id, run_date, ran_strategies, tier_counts: dict[str, int])` — message becomes e.g. 「…完成 3 個策略共 3 場討論,推薦 1 檔次、觀察名單 2 檔次」 (counts of pick-bearing sessions per tier; abstentions excluded). `_run_for_user` computes tier per completed slot from the discussion's conclusion (the slot runner returns/exposes the discussion — read `_run_strategy_slot` to find the cheapest place to read the final conclusion; if the conclusion isn't available in `_run_for_user`'s scope without a re-query, ONE targeted re-query of the day's rows before notifying is acceptable — it runs once per day).

- [ ] **Step 1: failing test** — patch `notify_user` with AsyncMock, call `_notify_daily_ready(..., tier_counts={"recommend": 1, "watch": 2})`, assert the message contains 推薦 1 and 觀察名單 2; a second test with `tier_counts={}` keeps the legacy message (no tier fragment). Follow existing notify tests' style if any exist (grep `_notify_daily_ready` in tests).
- [ ] **Steps 2-5**: fail → implement → green (existing auto_run tests must not regress; default `tier_counts=None` keeps old signature compatible) → ruff + commit — `feat(notify): daily-ready message splits recommend vs watch`

---

### Task 5: Scoreboard tier columns

**Files:**
- Modify: `backend/services/daily_scoreboard_service.py` (`build_scoreboard` entry dict)
- Test: `backend/tests/test_daily_scoreboard_service.py` (append)

**Interfaces:**
- Produces: per-strategy entry gains `recommend_decided`, `recommend_wins`, `recommend_win_rate`, `watch_decided`, `watch_wins`, `watch_win_rate` — decided-verdict denominators only (same rules as the existing win_rate; abstain/unverifiable/pending excluded), tier from `tier_for(d.conclusion)`; rows with tier None (edge: decided but conclusion mangled) fall into neither column (they still count in the untiered totals).

- [ ] **Step 1: failing test** — seed one strategy with 2 decided recommend-tier picks (1 win 1 loss) + 1 decided watch-tier win; assert `recommend_win_rate == 0.5`, `watch_win_rate == 1.0`, and the pre-existing untiered `win_rate` still counts all 3 (≈0.667). Reuse the file's existing discussion-seeding helpers.
- [ ] **Steps 2-5**: fail → implement (one pass over the group, reuse the existing wins/losses loop structure) → whole scoreboard suite green → ruff + commit — `feat(scoreboard): per-tier win rates`

---

### Task 6: Frontend grouping

**Files:**
- Modify: `frontend/src/pages/DailyPage.tsx` (+ its test file)

**Interfaces:**
- Consumes: `tier` on each run object (Task 3's payload).
- Produces: within each day/strategy listing, runs grouped 「推薦」 first (amber-accent header, SessionBadge visual language from #267) then 「觀察名單」 (muted); runs with `tier === null`/undefined render exactly as today (no group header) — backward compatible with pre-deploy payloads. Scoreboard component shows tiered win rates when the fields exist, with per-tier `n` (the n=1 lesson applies per tier — reuse the existing 樣本不足 marker logic for n < 10).

- [ ] **Step 1: failing tests** — extend the DailyPage test file (export pattern per #267): (a) runs with mixed tiers render 推薦 group before 觀察名單; (b) tier-less payload renders no group headers (legacy snapshot intact); (c) scoreboard row with `recommend_decided: 1, recommend_win_rate: 1.0` shows the 樣本不足 dimming for that column.
- [ ] **Steps 2-5**: fail → implement → `npx vitest run src/pages` + `npx tsc --noEmit` + lint → commit — `feat(frontend): recommend/watch grouping + tiered scoreboard`

---

### Task 7: PR + operator calibration

- [ ] **Step 1:** Full backend suites (two processes, CI-style) + frontend suite + `--collect-only` clean + ruff/lint/tsc.
- [ ] **Step 2 (operator, in container, read-only):** `python -m scripts.calibrate_pick_tier` — paste the sweep table into the PR body; if the selection rule picks a threshold ≠ 0.85, update `T_CONSENSUS` in the same PR (one commit, "calibration says X") and re-run Task 1/2 tests.
- [ ] **Step 3:** PR titled `feat(tier): confidence tiering for daily picks`. Body: objective-function context (total-alpha internal / signal-to-noise external), the sweep table, the no-behaviour-change guarantee, the 20-decided window statement, frontend screenshots optional.
- [ ] **Step 4:** CI green → merge. **Do NOT deploy** — bundles with the next user-approved window.
- [ ] **Step 5 (post-deploy acceptance):** first live day: page shows groups; notification names tier counts; scoreboard tier columns render with honest small-n dimming.

---

## Self-review notes

- **Spec coverage:** tier function (T1), calibration (T2 + T7 operator step), page (T6 via T3 payload), notifications (T4), scoreboard (T5), no-behaviour-change (global + every task's scope), post-maiden sequencing (global).
- **Type consistency:** `tier_for(conclusion) -> str | None` consumed identically in T3/T4/T5; payload field `tier: str | None`; sweep pick dicts as defined in T2.
- **Adaptation points flagged:** payload serializer shape (T3), conclusion availability in `_run_for_user` (T4), existing seeding helpers (T5), export/test pattern (T6).
