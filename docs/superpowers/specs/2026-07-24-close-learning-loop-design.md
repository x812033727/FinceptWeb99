# Design: Close the daily-recommendation learning loop

**Date:** 2026-07-24
**Status:** Approved (brainstorming) — pending implementation plan
**Owner scope:** live auto-run owner `x812033727@gmail.com` (admin)

## Problem

The daily AI stock recommendations feel inaccurate and "miss a lot." Measured over
100 auto-run discussions (2026-04-24 → 07-24):

| verdict | count |
|---|---:|
| abstain | 47 |
| unverifiable (mostly "synthesizer returned no symbols") | 26 |
| ungraded (null) | 14 |
| win / big_win | 6 |
| loss / big_loss | 7 |

Only ~13/100 produced a gradeable pick (~50/50). The complaint is really two things:
the system rarely commits to a verifiable pick ("沒估算到"), and the few it makes are
coin-flips ("不準").

The user's chosen focus: **fix the learning loop first** — without gradeable outcomes
feeding a working loop, no tuning compounds.

## Diagnosis

The learning-loop machinery is ~90% built (from the R2 round) but is **starved and
partly unwired**:

- `discussion_lessons`: **1086 rows, all `episodic`, 0 `semantic`.** Promotion has
  never fired.
- Root cause of 0 promotions is twofold:
  1. `promote_eligible_lessons` (lesson_tier_service) is **never scheduled** — only
     reachable via an admin endpoint.
  2. Promotion needs `usage_count >= 5 AND hit_count/usage_count >= 0.6`, but
     `hit_count` only bumps on a `win` verdict — and wins are rare (6/100), so the
     hit-rate can't reach 0.6.
- `post_mortem` self-critique runs only in the **backtest/sweep** path, never on
  **live** auto-runs (0 live post-mortems).
- Calibration is fitted at the end of a sweep and applied live via the strategy
  template's `calibration_curve` column (`synthesizer._apply_calibration_to_conclusion`)
  — the feedback path exists, but no sweep has fitted a curve, and the sample floor
  (`MIN_SAMPLES_FOR_FIT = 30`) is far from met.
- Lessons are **owner-scoped** (`shared_owner_ids`), so backtest-generated lessons only
  reach live if the sweep runs under the same owner.

**Core insight:** the loop is starved because the system rarely produces a gradeable
outcome. Fuel (graded outcomes) unlocks everything downstream, so fuel comes first.

## What already exists (do NOT rebuild)

- `backtest_sweep_service`: sweep runner with CRUD + worker + cancel + concurrency +
  budget. Per date it already runs post-mortem passes (`_run_post_mortem_pass`),
  extracts win + miss lessons, and **refits the isotonic calibration curve** from the
  rolling `(confidence, outcome)` pool.
- R2's `as_of` clamps guarantee look-ahead safety on every replayed dimension.
- `synthesizer._apply_calibration_to_conclusion` already reads the template curve live.
- Lessons are already cited and fed back into prompts (323 lessons cited, 1008 hits).

## Hard constraint: archive depth

Full-context replay is bounded by the shallowest required dimension:

| table | earliest | usable depth |
|---|---|---|
| ohlcv_daily (TW) | 2021-03 | deep |
| tw_institutional_daily (chip) | 2025-07 | ~1 year |
| tw_margin_daily | 2026-04-01 | ~80 days (extendable via #248 legacy dated surface) |
| **fundamentals_snapshots** | **2026-04-20** | **~71 days (hard floor — no historical snapshot source)** |

→ Usable full-context replay range ≈ **2026-04-20 → present, ~60-65 trading days**. At
~10-15% commit rate, a full sweep yields only **~6-10 graded outcomes per strategy** —
below the calibration threshold of 30.

**Decision (user-approved):** accept gradual accrual. Phase 1 fuels the things that
*are* reachable now (semantic lessons: threshold is usage≥5 + hit-rate, not 30; and
post-mortem accumulation). Calibration full-fit is a **longer-horizon goal** fed by
backtest + live samples accruing over time — not gated on, not force-fit, and no
threshold lowering and no fundamentals backfill in this project.

## Goal & success criteria

Close the loop so knowledge compounds:

- **Phase 0 done:** promotion is scheduled; live auto-runs generate post-mortems;
  sweep/live share an owner scope. (Unit-tested.)
- **Phase 1 done:** a bounded sweep under the live owner has run; `semantic` lessons
  count is > 0 and growing; graded-outcome count materially up.
- **Phase 2 done:** semantic lessons are cited by live discussions; abstention rate and
  graded win-rate measured before/after. Calibration curve fit is *tracked* (sample
  progress toward 30) but not required.

## Approach (fuel-first, phased single project)

### Phase 0 — plumbing (cheap, low-risk, ships first)

- **②a Schedule promotion.** Add a scheduler job that calls
  `promote_eligible_lessons` per market on a cadence (e.g. weekly). This is the direct
  cause of 0 promotions.
- **②b Review promotion criteria.** Do NOT change yet. Flag: `hit_count` bumping only
  on `win` plus a 0.6 hit-rate may be too strict. Only adjust if, after Phase 1 fuel +
  the schedule, semantic count is still 0. (Any change ships as its own reviewed PR.)
- **④ Live post-mortem.** Wire the sweep's `_run_post_mortem_pass` equivalent into the
  live verify path (`verify_discussion_outcome`) so live discussions also produce
  post-mortems + lessons. Guard for cost (one extra LLM call per verified discussion)
  and only on decided (non-abstain) outcomes.
- **⑤ Owner alignment.** Confirm the sweep runs under `x812033727@gmail.com` (or a
  `shared_owner_ids`-shared owner) so its lessons reach live. Calibration lives on the
  template (global) so it already reaches all.

### Phase 1 — fuel (the only heavy-cost part)

- **① Bounded sweep** under the live owner over the ~60-day usable range (chip +
  fundamentals present). The existing engine auto-produces graded outcomes,
  post-mortems, win/miss lessons, and refits calibration where samples allow.
- **Budget cap + batching.** Set an explicit LLM-spend / session ceiling. Use the
  existing cancel/concurrency support to run in resumable batches; budget exhaustion
  stops the batch, never corrupts the loop.
- **Dry-run first.** Run a tiny slice (a few sessions) to confirm the pipeline wires
  end-to-end before scaling.

### Phase 2 — verify feedback + measure

- Confirm `calibration_curve` is applied at live synthesis (where a curve exists) and
  that `semantic` lessons are cited by live discussions.
- Before/after measurement: abstention rate, graded win-rate, sample counts, semantic
  lesson count. No new features — acceptance/measurement only.

## Data flow (once closed)

```
historical session → sweep replay (3 strategies × LLM discussion)
  → graded outcome + post-mortem → win/miss lessons (owner-scoped)
  + refit calibration_curve (template column, where samples allow)
  → [scheduled] episodic → semantic promotion
  → live discussion cites semantic lessons
  → live synthesis applies calibration_curve → better-calibrated confidence
  → live outcome graded → feeds the loop again (compounding)
```

## Error handling

- Sweep already has cancel / concurrency / budget guards; budget exhaustion halts the
  batch cleanly.
- Look-ahead safety is guaranteed by R2's `as_of` / `end=as_of` clamps on every
  replayed dimension — unchanged here.
- Promotion is pure SQL over existing rows; scheduling it is low-risk and idempotent.
- Live post-mortem must fail-closed: a post-mortem error must not block or corrupt the
  verdict-writing it hangs off.

## Testing

- **Phase 0:** unit tests for the promotion schedule trigger, the live post-mortem
  wiring (fires on decided outcomes, skipped on abstain, fail-closed), and owner-scope
  correctness.
- **Phase 1:** a small dry-run slice validates the pipeline before scaling; assert the
  sweep produces graded rows + lessons under the correct owner.
- **Phase 2:** before/after metric queries as the acceptance gate.

## Out of scope (named, not done)

- Reducing abstention / making the panel commit more often (a separate project; the
  biggest lever on raw accuracy but deliberately deferred).
- Fundamentals historical backfill (no source; hard floor accepted).
- Lowering `MIN_SAMPLES_FOR_FIT` (rejected — small-sample isotonic overfits).
- Changing promotion criteria (contingent on Phase 1 result; own PR if needed).

## Open questions for implementation plan

- Phase 1 LLM-spend ceiling / number of sessions to sweep (sizing the budget cap).
- Promotion schedule cadence and market coverage.
