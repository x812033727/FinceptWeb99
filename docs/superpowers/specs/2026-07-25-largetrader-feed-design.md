# Design: Large-trader futures positioning feeds price_signal

**Date:** 2026-07-25
**Status:** Approved (brainstorming) — database-map Step 2, first candidate.
Implementation queued behind the in-flight SDD pipelines; this spec sets the
pattern every later finmind-schema feed will follow.

## Context

The database map found the entire `finmind` schema write-only: 39 populated
datasets no application code reads. The ranked Step-2 candidate list put
futures positioning first: `price_signal` is the strategy whose macro veto is
being converted to a downgrade (pick-governance spec), and the panel's
personas are *already trying to cite* signals of this class — the experiment
transcripts log hallucination warnings for exactly these missing fields.

What the panel already sees: `taifex_positioning` (三大法人 TX net OI + 5-day
change, via live FinMind HTTP with a 4h cache — `blocks/derivatives.py`).

What it cannot see, sitting in the archive:

| Table | Rows | Signal |
|---|---|---|
| `finmind.tw_futures_oi_largetraders` | 682 | 大額交易人 top-5 / top-10 long/short OI, incl. 特定法人 (`*_special`) — market structure & concentration, complements the institutional aggregate |
| `finmind.tw_futures_dealer_volume` | 26,385 | per-dealer daily futures volume — breadth/turnover context |

## The change

**1. A reader service for the finmind schema** — `services/tw_derivatives_archive.py`.
Raw-SQL (schema-qualified `finmind.` — no ORM models exist app-side and none
are added) reads with an `as_of` clamp (`ts <= :cutoff`), returning:

- `large_trader_positioning(as_of|None) -> dict | None`: latest session's TX
  top-5/top-10 long/short OI + 特定法人 share, plus 5-session deltas; None
  when the archive has nothing (the panel's standard "signal unavailable"
  shape).
- Dealer volume folds in as one breadth line (total volume vs 20-session
  mean), not a per-dealer dump — personas need a signal, not a ledger.

This service is the **pattern-setter**: archive-only (the data is already
ingested three times daily — no live fallback, a gap simply reads None and is
visible in the freshness monitor), backtest-safe by the same `ts <= as_of`
clamp every other block uses.

**2. A context block** — `fetch_large_trader_positioning` in
`blocks/derivatives.py`, writing `ctx["large_trader_positioning"]`, error-
isolated like its siblings.

**3. Strategy routing (the structural piece).** `build_market_context` gains
`strategy: str | None = None` (threaded from the auto-run caller the same way
`focus_symbols` is). The new block runs only when `strategy == "price_signal"`.
Manual/other discussions (strategy None) skip it — cost stays where the
evidence is. Backtest replays pass their strategy too, so replay parity holds.

**4. Persona exposure.** `large_trader_positioning` joins the same profiles
that already carry `taifex_positioning` (macro / quant / contrarian /
short-term sets in `persona_config.py`) — no new profile logic.

**5. Prompt annotation.** One entry in `_BLOCK_ANNOTATIONS` describing the
block's fields in the panel's schema language, so personas cite it instead of
hallucinating it.

## Measurement (before/after, honest)

- Replay A/B on the same stratified sessions used by the strengthening work:
  price_signal with and without the block (the replay flags from #263 make
  this a rules-free, code-flagged comparison — implementation detail for the
  plan: a `--disable-block` style env/flag or two replay batches around the
  merge boundary).
- Watch: abstention rate, hallucination-warning count for derivatives-class
  signals (should drop), and D5 excess of picks. n will be small; this gates
  "keep/expand to next candidate", not a rules change.

## Out of scope

- The remaining Step-2 candidates (借券→chip_quality, 處置股→risk, 產業鏈,
  外資持股, 可轉債) — each gets its own thin spec after this one's
  measurement reads.
- Any new ingest (the data already lands).
- Options-side tables (`tw_option_dealer_volume`) — second pass if the
  futures-side measurement earns it.
