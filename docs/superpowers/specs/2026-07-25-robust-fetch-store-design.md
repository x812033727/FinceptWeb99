# Design: Robust data fetching and storage

**Date:** 2026-07-25
**Status:** Approved (brainstorming)

## Context

Follows the data-fetch stabilization work (#265: archive-first reads, honest
FinMind run outcomes, content-level freshness monitor). A full health check on
2026-07-25 surfaced the remaining gaps this design closes:

- **Chip data is structurally one day older than price data.** Institutional /
  margin ingest runs at 17:10 Taipei; TWSE often publishes the same session's
  ledgers later that evening. The run finds nothing, reports `ok 0 rows`, and
  the data lands on the NEXT day's run — so the 04:00 discussion reads
  yesterday's prices beside the day-before-yesterday's chips.
- **`ok 0 rows` is ambiguous at the task level.** It currently means any of:
  nothing was due (fine), the source hasn't published yet (expected, retry
  tonight), or the source silently broke (the failure class this repo keeps
  being burned by). #265 fixed this ambiguity for the FinMind market-wide
  runner; the per-task ingest jobs still conflate all three.
- **A 4xx from FinMind permanently kills a dataset even when a fallback
  exists.** `TaiwanStockBuyBack` has returned 422 daily for as long as logs
  reach; `tw_stock_buyback` has **zero rows ever**; a TWSE self-crawl fetcher
  (`t187ap43_L`) exists in the codebase and is registered for exactly this
  dataset — but client errors never route to it.
- **The two biggest tables grow without any storage policy.**
  `tw_stock_shareholding` 433 MB over ~10.5 months (~40 MB/month);
  `news_articles` 147 MB over ~8 months (~18 MB/month).

## Decisions taken (with the user)

- **Raw rows are never deleted — compression only.** Backtests must be able to
  reproduce historical context; deleted rows are unrecoverable, and the disk
  has 94 GB free. Any future deletion proposal is a separate, per-table,
  explicitly-authorized decision.
- **Off-host backup replication is deferred** (raised in the health check —
  dumps currently live on the same disk as the database — but explicitly out
  of this round's scope by user choice).
- Three tracks in one effort; Track 2 consumes Track 1's semantics.

## Track 1 — Unified fetch-failure regime (拿)

One doctrine, applied first to the two live defects, then available to every
ingest job:

**Outcome taxonomy.** A run's health record must self-classify, extending the
`idle:` / `partial:` prefix convention #265 established for the market-wide
runner:

| Outcome | Meaning | Health |
|---|---|---|
| `ok` (rows > 0) | did work | ok |
| `idle` | nothing was due | ok, `error="idle: …"` |
| `not_yet_published` | asked for TODAY's session, source has no rows for it yet | ok, `error="not_yet_published: <session>"` |
| `gap` | asked for a PAST session and got nothing — data that should exist doesn't | **not ok** — this is the silent-failure class |
| `failed` | transport/API error | not ok |

The discriminator between `not_yet_published` and `gap` is purely the
requested session's age: same-day emptiness is expected before the source's
publish hour; day-old emptiness is a defect. No guessing about upstream state.

**Retry doctrine.**

- **429 / 5xx / timeouts** — transient: bounded backoff retry (the existing
  Redis backoff infra; no new machinery).
- **4xx other than 429** — permanent for that request shape: do NOT retry the
  same source. If the dataset has a registered self-crawl fallback, run it in
  the same chunk; only when no fallback exists does the chunk report `failed`.

**First application (the buyback fix).** In the FinMind chunk runner, a 4xx
from the API for a dataset present in the self-crawl registry routes to the
self-crawl fetcher within the same run. `TaiwanStockBuyBack` → `_fetch_buyback`
(TWSE `t187ap43_L`) is the proving case: `tw_stock_buyback` goes from
permanently empty to populated with zero new schedules. The commented-out
`ingest_buyback_tw` scheduler block is removed rather than enabled — one
mechanism, not two.

**Second application.** `ingest_institutional_tw` / `ingest_margin_tw` adopt
the taxonomy so their daily 17:10 empty-handed run records
`not_yet_published: <today>` instead of a work-lookalike `ok 0 rows`, and a
past-session hole records `gap` (not-ok) so the dashboard finally alarms on
the class of failure that has historically been silent.

## Track 2 — Same-day chip re-probe (拿)

A second daily run of `ingest_institutional_tw` and `ingest_margin_tw` at
**21:40 Taipei (13:40 UTC)** — after TWSE's evening ledger publication. The
walk-back design (`pending_market_days`) makes re-runs idempotent: the evening
run picks up exactly what the 17:10 run classified `not_yet_published`.

Effect: the 04:00 discussion reads T-1 chips beside T-1 prices instead of T-2.
Scheduler entries are new ids (`…_evening`) invoking the same `run()`;
`max_instances=1` + the existing per-job locks prevent overlap.

Acceptance is measurable: the freshness monitor (03:00 Taipei, from #265)
should stop reporting `institutional_tw` / `margin_tw` one session behind
`ohlcv_tw` on normal weekdays.

## Track 3 — Big-table compression, no deletion (存)

**3a — enable TimescaleDB compression on the existing chip/price hypertables**
(`ohlcv_daily`, `tw_institutional_daily`, `tw_margin_daily`): compress chunks
older than 90 days, `segmentby` symbol, `orderby` ts. Reads stay transparent;
backtest access to compressed ranges is verified by test (a
`read_ohlcv_range` over a compressed window must return identical rows).

**3b — convert `tw_stock_shareholding` to a hypertable and compress.** Its PK
already contains the time column (`market, symbol, ts, bucket_id`), so
`create_hypertable(..., migrate_data => true)` is clean. 433 MB and weekly
cadence make it the single highest-value target; typical columnar compression
on this shape is ~90%. The conversion runs as a migration at deploy (minutes
of exclusive lock on a table only batch jobs touch — the deploy window
already tolerates this).

**3c — `news_articles` is explicitly deferred.** Its PK is `id` alone, so
hypertable conversion requires PK surgery on a table with FK consumers, while
growth is only ~18 MB/month. The risk/benefit is upside-down today; revisit
when growth or query pain changes.

## Sequencing and safety

1. Track 1 taxonomy + buyback fallback (pure code, test-first).
2. Track 2 schedules (depends on Track 1's `not_yet_published`).
3. Track 3a policies (migration), then 3b conversion (migration, deploy-window).
4. Implementation starts **after** the running veto-relaxation experiment
   completes and #265's deploy is accepted — migrations restart containers,
   and the experiment's replay process lives in one.

Migration rule (standing, from R6): every migration runs against an ephemeral
`postgres:16` with a production-shaped schema before merge.

## Out of scope

- Off-host backup replication (deferred by user choice — recorded above).
- Any row deletion.
- `news_articles` conversion (3c rationale).
- Feeding the finmind-schema silo into AI context (that is the database-map
  effort's Step 2, its own spec).
- The parked #265 residual (heterogeneous-batch phase-downgrade false
  positive) — stays in the fast-follow queue, unrelated to ingest.
