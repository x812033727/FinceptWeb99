# Close-learning-loop — before/after metrics

## Phase 0 (shipped, PR #256, deployed ff08307)

- `run_post_mortem_pass` shared between sweep and live.
- Live verify path runs post-mortem on decided verdicts (fail-closed).
- Weekly `promote_lessons` job scheduled + live (verified REGISTERED in running scheduler).
- Critical bug caught by whole-branch review (C1): `build_post_mortem_message`
  raised on `as_of_date=None`, making live post-mortem dead-on-arrival — fixed
  with a forward anchor (`as_of_date or to_tw_date(created_at)`).

## Phase 1 dry-run (2026-07-25)

Replayed 3 stratified uncovered sessions under the live owner:

    python -m scripts.replay_daily_discussions --dates 2026-04-30,2026-05-20,2026-06-12 --budget-usd 25

- **3 sessions, 9 discussions, US$11.33, 0 produced nothing** (pipeline works end-to-end).
- Graded outcomes after verify: **0 / 9 — all 9 ABSTAINED.**
- Post-mortems: 0. New semantic lessons: 0 (still 0 total).

### Why zero fuel — the key finding

The abstentions are **genuine reasoned abstentions**, not the "synthesizer returned
no symbols" broken path. Every discussion produced a full conclusion; each got 5
real candidates and ran the full 5-round debate, then judged that none met the entry
bar (量能突破 + 法人籌碼同向 + 風報比≥3:1 + 系統性風險可控). All three sessions sat in a
bearish regime (外資期貨淨空, 法人連日賣超, high 台VIX percentile) — conservative
abstention is arguably correct there.

### Two implications (empirically confirmed)

1. **Abstention is the binding constraint** on BOTH accuracy and fuel. Even backtest
   mostly abstains (consistent with R2's 8 picks / 78 sessions). Fueling the loop
   needs either a much larger (costlier) sweep to catch the ~10% of sessions with
   picks, or a reduction in unwarranted abstention.
2. **A concrete "沒估算到" instance:** 2026-05-20 chip_quality abstained partly because
   the broker-concentration (分點) data was **11 trading days stale** in replay — the
   panel abstained not because the setup was bad but because it could not SEE the
   signal. Data-availability gaps directly cause abstentions.

### Conclusion

Phase 0 plumbing is done, correct, and live. The dry-run cheaply proved that the
loop's real bottleneck is abstention — part data-driven (missing/stale signals =
"沒估算到"), part regime-driven (correct conservatism). Reducing unwarranted
abstention is the highest-leverage next lever for both accuracy and fuel; it was
scoped out of this project and should be its own effort.

### What came next (added on merge)

That effort ran as the strengthening workstream — see
`2026-07-25-strengthening-findings.md`. It confirmed this conclusion on a much
larger sample: over n=168 archived abstentions, the top-3 candidates
`price_signal` declined beat TAIEX by **+9.20pp** (64% beat the index), while
`chip_quality`'s caution was vindicated (+0.47pp, no missed alpha). So
"over-abstention" is real but **concentrated in one strategy**, not global —
a sharper target than this document could name at n=3 sessions.
