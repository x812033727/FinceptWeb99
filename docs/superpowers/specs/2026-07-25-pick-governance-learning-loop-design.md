# Design: Pick governance (macro-veto downgrade) + learning-loop hit semantics

**Date:** 2026-07-25
**Status:** Approved (brainstorming) — adoption criteria pre-registered BEFORE the
experiment's grades are known.

## Context

Prediction accuracy = signals seen × judgment × pick selection × learning loop.
This design covers the two levers with the strongest evidence:

- **Lever 1 (pick selection).** `price_signal`'s macro veto is the binding
  abstention mechanism: baseline replays abstained 15/15 on the sampled
  sessions; the controlled veto-relaxation experiment (same sessions, same
  panel, only the veto clause relaxed) has picked in 7/7 completed sessions so
  far. WS2 measured the declined top-3 at **+9.20pp excess vs TAIEX (64% beat,
  n=168)**. The alpha is measured; what's missing is a *governed* way to take it.
- **Lever 3 (learning loop).** 1,086 episodic lessons, **zero semantic
  promotions ever, across two generations of the stack**. Root cause is scoring,
  not fuel: `hit_count` increments only on winning verdicts while `usage_count`
  increments on every citation, so in a system that correctly abstains most days
  the ratio (max observed 0.259) can never reach the 0.6 promotion floor.
  **Correct abstention is currently scored as failure.**

These interlock: lever 1 makes the panel pick more; lever 3 makes the system
learn from both its picks and its refusals.

## Part 1 — Macro veto becomes a governed downgrade

### The change

The production rules text (`discussion_auto_run_configs.rules`, a DB row — no
deploy needed, instantly revertible) gains a clause **scoped by its own wording
to 量價訊號 (price_signal) sessions only**:

> 【僅適用於量價訊號策略場次】總經逆風(外資台指期淨空、三大法人連續賣超、
> 台VIX 偏高等系統性風險)不得作為否決個股的唯一理由。當候選同時滿足技術面
> 與籌碼面進場條件時仍應給出推薦,但總經逆風時必須:(1) 建議部位上限減半;
> (2) 停損位收緊並明確標出;(3) 在 risks 首條標注「總經逆風環境」。
> 僅當個股本身不符技術或籌碼條件、或風報比不足時才棄權。

This is deliberately a *downgrade*, not a deletion: the conservative machinery
survives as position-sizing and risk-labeling discipline.

### Pre-registered adoption criteria (decided now, applied to tonight's grades)

Grade the 15 experiment sessions' picks on D5 vs TAIEX (the WS2 method):

| Outcome | Action |
|---|---|
| mean excess > 0 **and** big_loss (any close ≤ −5%) share ≤ 25% of picks | **Adopt** the clause above |
| mean excess > 0 but big_loss share > 25% | Adopt with 部位上限改為 1/3 (tighter sizing) |
| mean excess ≤ 0 | **Do not adopt**; document and stop — the veto was right |

n≈15-20 picks is directional, not proof; that is why adoption ships with the
revert guard below rather than waiting for unattainable sample sizes.

### Governance: audit + revert guard

- The pre-change rules text is archived (timestamped file in `docs/` +
  the DB update runs through a one-off script that prints the old text) so
  revert is one UPDATE.
- **Auto-alert revert trigger** (monitored by the existing
  `monitor_strategy_health` job): if live price_signal accumulates **3
  consecutive decided losses** or **2 big_losses within any rolling 10 decided
  picks** after adoption, raise a not-ok health record naming the revert
  instruction. Revert itself stays a human action (one DB UPDATE) — automation
  that rewrites trading rules unattended is a bigger risk than the delay.
- **Leakage watch:** the clause is prompt-scoped, so chip_quality/general could
  drift. Monitor their abstention rates for 2 weeks post-adoption; a drop
  > 20pp versus the prior 30 days triggers investigation and, if confirmed,
  the structural fix (a per-strategy rules column) becomes its own effort.

## Part 2 — Correct abstention counts as a hit

### The change

`record_lesson_outcome` currently runs only on winning verdicts. It gains a
second qualifying condition:

> A discussion with `verdict='abstain'` whose verified
> `pool_performance.avg_return_pct < 0` counts as a hit for every lesson its
> rounds cited — the lessons that argued for caution were right.

The data already exists: the verifier computes `pool_performance` for abstain
rows today (verified at `tasks/verify_discussion_outcome.py:291`), so this is a
gate change, not new measurement.

Symmetrically honest: an abstention over a pool that **rose** simply does not
increment hits (usage still counts) — the current behaviour, now meaning
"missed gain" instead of blanket failure.

### What does NOT change (yet)

- The 0.6 promotion threshold and the ≥5 usage floor stay. With abstention hits
  counted, the ratio becomes reachable for genuinely good lessons; if promotion
  is still starved after ~2 weeks of live + experiment grading, threshold
  tuning becomes its own evidenced decision, not a blind knob-turn.
- The weekly Sunday promotion job stays as-is (#256).
- Experiment rows (`--rules-override`, tagged on `candidate_snapshot`) remain
  excluded from the loop entirely (#264's guard is untouched).

### Measurement

Backfill question the implementation must answer before changing the gate: how
many of the 1,086 episodic lessons WOULD cross 0.6 under the new semantics,
computed retroactively over already-verified abstain rows? That number lands in
the PR body — if it is 0, the semantics change alone is insufficient and the
threshold discussion happens immediately instead of two weeks later. (The
retroactive pass only recomputes counters; it does not promote — the Sunday job
does that on its schedule.)

## Sequencing

1. Tonight: grade the experiment (read-only script, already written) →
   apply the pre-registered criteria table.
2. If adoption: Part 1's rules UPDATE + audit archive + revert-guard monitoring,
   then Part 2 (they are independent code paths; Part 2 proceeds even if
   Part 1 lands in the "do not adopt" row).
3. Both changes ride the normal PR flow; the rules UPDATE itself is an operator
   action recorded in the PR body.

## Out of scope

- Feeding new datasets to the panel (database-map Step 2, its own effort).
- Persona-level accuracy weighting, multi-sample voting, deeper fundamentals
  backfill (documented as future levers; no work here).
- Calibration-threshold changes (accrues data from this work; decisions later).
- Any change to backtest/replay semantics.
