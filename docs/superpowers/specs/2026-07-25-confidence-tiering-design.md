# Design: Confidence tiering for daily picks

**Date:** 2026-07-25
**Status:** Approved (brainstorming via grill-me session)

## Context — the objective function this serves

The grill-me session pinned the user's real objectives, superseding the raw
"raise hit rate" framing:

1. **Internally: maximize total excess return.** The veto downgrade (adopted
   2026-07-25, pre-registered criteria met: +13.37pp mean excess, 0/19
   big_loss) moves the system from ~0 picks to near-daily picks. Overall win
   rate settling to 60-70% is an accepted mathematical consequence, not a
   regression.
2. **Externally: maximize signal-to-noise.** Near-daily picks means the reader
   needs to see *which* picks deserve attention. Efficiency = 雜訊比, not LLM
   cost (cost work explicitly deferred until a replay measurement justifies it).
3. **Evaluation discipline (binding):** no rule changes until 20 decided
   verdicts accumulate, except a revert-guard trigger. Recorded in long-term
   memory; this spec must not violate it — tiering changes *presentation*,
   never picking behaviour.

## The change

A pure, read-time tier function — **no schema change, no behaviour change**:

```
tier(conclusion) -> "recommend" | "watch"
```

Derived at render/notify time from the stored conclusion JSON, so it applies
retroactively and consistently to every historical pick, and tier-threshold
tuning later never needs a migration or backfill.

### Tier basis (MVP — existing signals only)

A pick session is **recommend** when ALL of:
- `consensus_score >= T_consensus` (provisional 0.85 — see calibration below)
- hallucination warnings referencing signals the pick's reasoning relies on
  are ≤ `T_halluc` (provisional: 2, counted from
  `quality_signals.hallucination_warnings`)
- at least one recommended symbol carries both a technical signal
  (`signal_type` present in the candidate snapshot) and chip confirmation
  (the conclusion's reasoning cites 法人/籌碼 confluence — MVP proxy: the
  candidate row exists in the snapshot with a strategy_score, which already
  encodes chip factors for chip-bearing strategies)

Otherwise **watch**. Abstentions have no tier (nothing to rank).

### Initial threshold calibration — data exists TODAY

The 15 experiment sessions carry graded outcomes (19 picks, D5 vs TAIEX per
pick) AND full conclusions with consensus scores and quality signals. Before
wiring the UI, a read-only script sweeps `T_consensus` over the graded picks
and reports per-threshold: recommend-tier size, tier win rate, tier mean
excess. The threshold that keeps tier-win-rate ≥ 80% with ≥ ⅓ of picks in the
recommend tier wins; ties break toward the larger tier. If no threshold
achieves 80%, ship the best available and record the honest number — the
provisional values above are fallbacks, not conclusions.

Recalibration is allowed as live picks accrue **because tiering is
presentation** — the 20-decided rule guards picking rules, not display
thresholds. Each recalibration is recorded (PR or ops note) with the sweep
output.

## Surfaces

1. **Public daily page**: picks grouped 「推薦」 / 「觀察名單」 (recommend
   first, amber-accent vs muted; reuses the SessionBadge visual language from
   #267).
2. **Notifications** (`_notify_daily_ready` fan-out): the message names the
   recommend-tier count; watch-tier is mentioned only in aggregate ("另有 N
   檔觀察名單").
3. **Scoreboard**: per-strategy rows gain tiered win-rate columns
   (`recommend_win_rate` / `watch_win_rate`, same decided-denominator rules as
   #244's lenses; `n` shown per tier — the n=1-shows-100% lesson applies
   per-tier too).

## What does NOT change

- Picking, abstention, rules text, verdict grading, learning loop, guard —
  untouched. Tier is computed after the fact from stored JSON.
- The 20-decided evaluation window counts ALL decided picks regardless of tier.

## Testing

- Pure tier function: table-driven over conclusion fixtures (high consensus /
  low consensus / hallucination-heavy / missing quality_signals → watch, not
  crash; missing consensus_score → watch).
- Calibration script: pure sweep logic tested with fixed fixtures; the
  real-data run is an operator step whose output lands in the PR body.
- Scoreboard: tier columns tested against the same fixtures as the existing
  lens tests; public page: badge-style grouping component tests (pattern from
  #267).

## Sequencing

After the Monday maiden-run acceptance (2026-07-28). The first live days run
untier ed on purpose: the calibration sweep gains live picks to validate the
threshold chosen from experiment data before the UI split ships.

## Out of scope

- Calibration-curve integration (needs the 30-sample floor; accrues naturally).
- Persona accuracy weighting, multi-sample voting, LLM cost reduction (each
  gated on its own measurement).
- Any change to what the panel does.
