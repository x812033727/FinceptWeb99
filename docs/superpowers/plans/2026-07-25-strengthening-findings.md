# Strengthening effort — findings

## WS1 — Pipeline audit (3 parallel adversarial auditors)

Two confirmed defects; the conclusion-key layer verified clean (single writer
`_safe_conclusion` always writes both `recommended_symbols` and `recommendations`;
`abstained` always set since #233).

- **B (fixed, #261): public scoreboard blended backtest replays into the live track
  record.** `build_scoreboard` filtered owner+auto_run+done but not `as_of_date`; backtest
  rows run under the same owner. Live DB was aggregating **87 backtest vs 14 live** rows —
  the public win rate was mostly hindsight, and every fuel sweep would pollute it. Fixed
  with `as_of_date IS NULL`.
- **A (open, tangential): US/GLOBAL discussions are permanently mislabeled `unverifiable`.**
  `verify_discussion_outcome` uses a TW-only symbol regex (`^\d{4,6}$`) and TW-only data
  fetch, so a US pick ("AAPL") is scheduled but stripped to `[]` at verify time →
  `unverifiable` forever. Real bug, but the daily strategies are all TW, so it does not
  touch the daily-recommendation scoreboard. Fix = branch verify on `d.market`. Deferred.

## WS2 — "Should we have abstained?" (n=168 real archived price outcomes)

For every backtest abstention, the actual D5 return of the **top-3 ranked candidates** the
panel declined, benchmarked against TAIEX over the same window:

| strategy | excess vs TAIEX | beat index | read |
|---|---|---|---|
| **price_signal** | **+9.20pp** | 64% | **over-abstains — leaves real alpha on the table** |
| general | +1.33pp | 52% | marginal |
| chip_quality | +0.47pp | 48% | conservatism justified (no missed alpha) |

Raw (un-benchmarked) top-3 return: mean +6.13%, 69% up, 44% cleared the +5% win bar;
price_signal top-3 rose +12.96% mean.

**Headline: over-abstention is real but concentrated in `price_signal`.** It abstains on
sessions where its own technical+chip candidates were strong, vetoed by a macro/systemic-risk
condition (外資期貨淨空 / 系統性風險), and those declined candidates beat the index by ~9pp.
`chip_quality`'s caution, by contrast, is vindicated — its declined candidates had no edge
vs the index.

**Caveats (hold honestly):** backtest periods may not span all regimes; top-3-by-score is
not exactly what the panel's full entry criteria would pick; still, +9.2pp excess for
price_signal is large and consistent enough to flag as the prime target.

**This is evidence-grade (n=168), not the n=5 live-accuracy noise.** Per the guardrail it
motivates a *future, carefully-designed* behavior change to price_signal's macro-veto —
NOT an immediate edit. The controlled test: re-run those abstained sessions with the veto
relaxed and grade the picks.

## WS4 — Live vs backtest parity (substantially answered)

The apparent divergence is explained without a panel-logic difference: (1) the verdict-labeling
artifact (fixed #258 — live abstentions were parked 5 days / mislabeled), (2) backtest mode
sees clamped, thinner context (info_cutoff + `_unavailable` blocks + a fundamentals archive
only back to 2026-04-20) so it is structurally more conservative, (3) different periods/regimes,
(4) tiny live n. No evidence of a panel-logic divergence surfaced.

## WS3 — Cheaper fuel (pending budget)

Not yet run. WS2 gives it direction: fuel should over-sample the sessions and strategy
(price_signal) where the over-abstention signal lives, so a future veto-relaxation test has
graded outcomes to measure against.
