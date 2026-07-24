# Design: Daily-recommendation strengthening

**Date:** 2026-07-25
**Status:** Approved (brainstorming)

## Context

Follows the learning-loop-closure work (#256) and the abstention-labeling fix (#258).
Key established facts:

- The daily-recommendation loop plumbing is fixed and live (Phase 0).
- Live accuracy is currently **unmeasurable** — only 5 graded live picks. The
  "live 20% vs backtest 62%" gap is noise; do not build on it.
- The apparent "live never abstains" was a **measurement artifact** (abstentions were
  labeled late / mislabeled); live actually abstains ~55%.
- Abstention is mostly reasoned/correct; forcing more picks likely lowers accuracy.

## Guardrail (binding)

**Do not build any behavior change on n=5 accuracy data.** The goal of this effort is
to make accuracy *trustworthy and measurable* and to let the loop compound — evidence
before behavior changes.

## Workstreams (one coherent effort, sequenced by dependency)

### WS1 — Audit the scoreboard / verdict pipeline (foundation, first)

We just found one verdict-labeling bug (abstentions parked 5 days). There are likely
siblings. Systematically audit the verdict-writing path (`verify_discussion_outcome`),
the scoreboard aggregation (`daily_scoreboard_service`), and the public surface
(`public_daily`) for correctness of every verdict transition and every published
metric.

Known suspects to check explicitly:
- `recommended_symbols` vs `recommendations` key drift (verify reads
  `recommended_symbols`; calibration/other code reads `recommendations`).
- `abstain` vs `unverifiable` classification and the `abstained` flag's consistency.
- D5-close vs verdict-band asymmetry (touched in #244) — any residual inconsistency.
- Win-rate / sample-count (`n`) computation on the scoreboard.
- Any metric that reads `abstain`/`unverifiable` as signal.

Method: parallel adversarial sub-agents, each auditing one slice; findings verified
before fixing. Deliverable: a verified findings list; fix confirmed bugs (each its own
PR → CI → merge). This is the foundation — accumulated numbers must be trustworthy.

### WS4 — Live vs backtest parity experiment (early; cheap, high-diagnostic)

Pick dates that ran BOTH live and as a backtest replay; diff (a) what data/context each
path saw, and (b) the conclusion (pick vs abstain, reasoning). Controls for regime, so
it isolates any REAL behavioral divergence beyond the labeling artifact. (R2 experience:
side-by-side live/replay context is the highest-yield diagnostic.)

Deliverable: a side-by-side comparison. If live and backtest make different calls on the
same date, that is a real bug to chase; if not, it confirms the divergence was purely the
labeling artifact.

### WS2 — Measure "should we have abstained" (needs WS1 done)

For each abstention, pull the candidate pool's actual D5 returns and classify the
abstention as "correctly avoided a loss" vs "missed a gain," per strategy and regime.

Deliverable: an abstention-quality report. Pure measurement — no behavior change. Only
if abstentions are systematically wrong does a behavior change get proposed (its own
future effort).

### WS3 — Faster / cheaper fuel (enabler, ongoing)

The loop and the measurements above are starved of graded samples. Reduce per-session
fuel cost (fewer rounds / personas, or the FAST-degraded model for fuel sweeps) and/or
select sessions smarter, then run a larger fuel batch within an explicit budget.

Deliverable: a cost-reduced sweep configuration + a larger fuel batch. Caution: do not
degrade fuel so far it stops matching live behavior (WS4 is the check for that).

## Sequencing

WS1 (audit, foundation) → WS4 (cheap parity diagnostic) → WS2 (measurement, needs
trustworthy verdicts) → WS3 (fuel, provides samples, runs alongside once budget is set).

## Out of scope

- Any behavior change to the panel's picking/abstaining logic (evidence first).
- Reducing abstention (deferred; abstention is mostly correct).
- Lowering calibration sample thresholds; fundamentals backfill.
