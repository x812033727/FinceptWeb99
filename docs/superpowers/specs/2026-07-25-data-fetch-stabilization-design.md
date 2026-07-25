# Design: Data-fetch stabilization for the daily discussion pipeline

**Date:** 2026-07-25
**Status:** Approved (brainstorming)

## Problem

The daily AI discussion runs at 04:00 Taipei and re-fetches TWSE / FinMind /
FRED / yfinance live, even though the same data was ingested into the database
between 14:30 and 19:30 the previous evening. Every discussion therefore depends
on four external APIs being up at 04:00, and an outage or a rate-limit becomes a
hole in the panel's context.

## What is already true

The scheduling half of the goal is largely built and correctly ordered.

- 49 scheduled jobs, ~25 of them `ingest_*`, writing OHLCV, institutional flows,
  margin, fundamentals, revenue, announcements, news, TW VIX, futures OI and ETF
  yields into the database.
- All TW ingest completes by 19:30 Taipei; the discussion runs at 04:00 Taipei
  the next day — an 8.5-hour margin. The ordering is not the problem.
- The four HTTP-bound context blocks (`screener`, `index`, `macro`,
  `focus_briefs` in `services/discussion/context/blocks/http.py`) **already have
  an archive-reading mode**, selected by one parameter. Backtest replays use it
  daily.

The gap is that live mode passes `info_cutoff = None` and so never uses it.

## Scope

The daily AI discussion pipeline only. User-facing pages keep their live quotes —
intraday prices on a stock page are a product requirement, and the discussion is
a pre-open inference that does not need them.

## Decisions taken

- **Archive-first with live fallback.** Normal operation reads the database; a
  block falls back to an outbound call only when the archive has nothing for the
  target session. Strict archive-only was rejected: a single missed ingest would
  become a blind spot for the panel, and blind spots cause abstention (see
  "Risk" below).
- **Fix the broken ingests in the same effort.** Moving reads to the database
  without repairing ingest would relocate the instability rather than remove it.

## Risk this design must not create

WS4 established that backtest mode is *more conservative than live* partly
because archive gaps become `_unavailable` context blocks, and a panel that
cannot see a signal abstains. `2026-07-24-close-learning-loop-metrics.md`
records a concrete instance: on 2026-05-20 `chip_quality` abstained because
broker-concentration data was 11 trading days stale in replay — not because the
setup was bad.

Over-abstention is the live problem this codebase is currently working on
(`2026-07-25-strengthening-findings.md`: `price_signal`'s declined top-3 beat
TAIEX by +9.20pp over n=168). A naive "read only from the database" change would
make abstention *worse*. The live fallback exists specifically to prevent that.

## Architecture

One seam. `services/discussion/context/builder.py` fans the four blocks out
concurrently, and all four take the same `as_of` parameter:

- live: `info_cutoff = None` → every block fetches live
- backtest: `info_cutoff = prev_trading_day(as_of)` → every block reads the archive

Three new pieces:

1. **Trading-session resolver** — answers "which session should this discussion
   read?". At 04:00 Taipei that is the most recent settled trading day. Reuses
   `services/tw_trading_calendar`.

2. **Archive-first wrapper** — because both modes are reachable through the same
   function signature, the fallback lives entirely above the services: call with
   `as_of=<session>`; if the result is empty, call again with `as_of=None`. **No
   service signature changes.**

3. **Source tagging** — each block records on `ctx.data_sources` whether the
   archive or the live fallback answered.

### The off-by-one that must not be got wrong

Backtest `as_of` means "data must be **≤ the previous** trading day" — it
excludes `as_of` itself, because the pick is entered at `as_of`'s open. Live
wants the opposite: **include** the most recent settled session. The resolved
session is therefore passed straight to the blocks while the builder's own
`as_of` stays `None`. That keeps the row classified as live (no 回測 badge, no
backtest gating) and avoids reading one day too far back.

## Components

| File | Change |
|---|---|
| `services/discussion/context/read_session.py` | **New.** Session resolver + archive-first wrapper |
| `services/discussion/context/builder.py` | Live mode passes the resolved session to the four blocks; `as_of` stays `None` |
| `services/discussion/context/blocks/http.py` | Blocks call through the wrapper and record `ctx.data_sources` |
| `services/stock_report_service.py` | Also calls `fetch_focus_briefs`; confirm its semantics stay unchanged |

## Data flow

```
scheduled ingest (14:30–19:30 Taipei, previous day) → database
                                                        ↓
04:00 discussion → resolver: "most recent settled session"
                                                        ↓
   four blocks ── try as_of=<session> ──→ rows → use, source=archive
                       │
                       └── empty ───────→ as_of=None live call, source=live_fallback
                                                        ↓
                       ctx.data_sources = {screener: archive, index: archive, ...}
```

**The fallback predicate reads the returned content, not the ingest health
record.** See the next section for why that distinction is load-bearing.

### "Empty" is not a strong enough predicate

The archive queries clamp `<= session`, so when the target day is missing they
return **the most recent earlier day** rather than nothing. A stale answer is
not an empty answer, so a naive emptiness check would silently serve week-old
data and never fall back — reproducing exactly the failure that made
`chip_quality` abstain on 2026-05-20 against 11-day-stale broker data.

The predicate is therefore **"did the archive answer *for the requested
session*"**, not "did it answer at all". Each block supplies its own accessor for
the session actually returned, because each already tracks it:

| Block | Session actually returned |
|---|---|
| `screener` | `max()` of each row's own `as_of` — the freshest row in the batch (see rationale below) |
| `index` | the bar's own date |
| `macro` | FRED `observation_end` |
| `focus_briefs` | the quote's session date per symbol |

`screener`'s answered-session accessor is deliberately `max()`, not `min()`:
a suspended symbol's stale bar sitting anywhere in a 200-row batch must not
make the whole batch look archive-dead — that would permanently disable
archive-first for the screener the moment one symbol halts trading. `max()`
mirrors live `STOCK_DAY_ALL` semantics, where the batch is treated as
answered once any row is current. This is a distinct signal from the
per-row `screener_actual_session` used for the phase-downgrade safety net
(`_maybe_downgrade_captured_session` in `builder.py`), which stays `min()`
— the *worst* row governs there, because that check exists specifically to
catch a batch containing anything stale. `focus_briefs` also keeps `min()`
for its own answered-session accessor (the "weakest answer" in
`fetch_focus_briefs`'s `_batch_session`), because a per-symbol brief batch
must fully answer for every symbol before it counts as archive-served —
there's no equivalent to "any one row being fresh enough" for a
per-symbol report.

A block whose returned session is older than the requested one is treated as a
miss and falls back, and the tag records `live_fallback` with the stale session
noted. Where a series is legitimately not daily (FRED macro publishes weekly or
monthly), the block declares its expected cadence and staleness is measured
against that instead — otherwise macro would fall back on every single run.

Backtest mode is unaffected: when `as_of` is set the strict archive path runs
with no fallback, or replays would see the future.

## Error handling

1. **Block level.** Empty archive → fall back. Both empty → the existing
   `record_error`, exactly as today. No new failure mode is introduced.
2. **No new exception paths.** The wrapper only calls an existing function a
   second time; exceptions are still absorbed per-block, so one bad block cannot
   take down the discussion.
3. **Observability.** `ctx.data_sources` is the long-term payoff: the next time a
   recommendation looks wrong, the first check is one table lookup instead of an
   investigation.

### The ingest health signal is unreliable in both directions

`finmind_tw_marketwide`, five consecutive days:

```
07:10  failed  ← wrote 15,629 rows
09:30  ok      ← wrote 0 rows
14:00  ok      ← wrote 0 rows
```

The run that does the work is labelled failed; the runs that do nothing are
labelled ok. `ingest_institutional_tw` reported `ok` with 0 rows on 2026-07-24,
indistinguishable from "already archived, nothing to do".

Any design that gates on job status would be built on sand. This is why the
fallback predicate inspects returned content.

## Ingest repairs

| | Work | Affects the TW discussion? |
|---|---|---|
| **R1** | Fix health-outcome semantics: partial success is not `failed`; "already archived, nothing to do" must be distinguishable from "broken, wrote nothing" | Indirectly — makes monitoring trustworthy |
| **R2** | Add a **content-level** freshness check reading `max(ts)` per dataset table rather than the job log | **Yes** |
| **R3** | Set `SEC_EDGAR_USER_AGENT_EMAIL` (currently `""` → SEC returns 403 → `ingest_announcements_us` has failed 90/90 runs over four days) | No — US-only data |

R3 needs a contact email from the operator; SEC requires a real one in the
User-Agent. It is sequenced last because it cannot touch the TW daily
recommendations.

Intermittent failures observed on `ingest_tw_vix` and `ingest_institutional_tw`
(2026-07-20/21) had already recovered by 07-22; R1 and R2 are what make a
recurrence visible rather than silent.

## Testing

**The invariant that matters most: backtest mode must never gain a live
fallback.** If it did, replays would see the future and every backtest-derived
conclusion — including the +9.20pp result now driving strategy work — would be
invalid. The test substitutes an exploding double for the live path, so any
outbound attempt fails loudly rather than being asserted away by a call count.

| Test | Pins |
|---|---|
| Archive has rows → archive used, tagged `archive`, zero outbound calls | Main behaviour |
| Archive empty → fallback fires, tagged `live_fallback` | Gap policy |
| Archive answers with an **older** session than requested → treated as a miss, fallback fires | The stale-not-empty hole; without this the whole design silently serves week-old data |
| A block whose series is legitimately non-daily (macro) does **not** fall back when within its declared cadence | Prevents a permanent fallback on every run |
| Both empty → existing `record_error`, no new failure mode | No regression |
| Resolver across weekday / weekend / holiday | The chip-metrics tests failed every weekend because they anchored on `date.today()` while the job walks weekdays only. The resolver is table-driven from fixed dates, never `date.today()` |

### Production acceptance

Green tests are not the acceptance gate. After deploy, run one real daily
discussion and read back `ctx.data_sources`: all four blocks should report
`archive`. Any block reporting `live_fallback` names an actual gap in the
scheduled data — which is R2's deliverable arriving for free.

## Out of scope

- User-facing pages' live quotes.
- Any change to panel picking/abstaining logic.
- Materializing a pre-built context snapshot (approach C) — larger surface than
  the problem warrants today.
