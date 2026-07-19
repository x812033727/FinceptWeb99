# FinMind Phase A' Cutover Runbook

Goal G2 — stop paying the FinMind subscription and fetch every Taiwan
dataset that has a working self-crawl connector **directly** from the
upstream (TWSE / MOPS / TAIFEX / TDCC / FRED).

This is a **live operation** against the deployed site's DB and against
real government endpoints (TWSE especially throttles). It is deliberately
NOT automated end-to-end — run it step by step and watch `status`.

## What "cutover" means

`dataset_sources.active_source` is the live routing knob the ingest
runner reads (`finmind/ingest/runner.py`). Flipping it from `finmind` to
e.g. `twse` is a one-row UPDATE — no code change. The
`finmind.scripts.cutover` tool performs that flip safely for every
dataset whose declared `fallback_source` has a wired connector.

**Currently shippable: 24 datasets** (the set `cutover` targets):

| source | count | examples |
|--------|-------|----------|
| twse   | 14 | Price, PER, DayTrading, Margin*, Institutional*, Shareholding, Delisting, BuyBack |
| mops   | 4  | MonthRevenue, FinancialStatements, BalanceSheet, CashFlows |
| taifex | 3  | FuturesDaily, OptionDaily, FuturesInstitutionalInvestors |
| tdcc   | 1  | HoldingSharesPer |
| fred   | 2  | GovernmentBondsYield, CrudeOilPrices |

The other ~35 TW datasets declare a fallback but have no handler yet
(new connector code — tracked for R9 long-tail). The flip gate
(`covers_dataset`) refuses to flip those, so you can't accidentally break
ingest by cutting one over early.

## Prerequisites

1. `python -m finmind.scripts.init_db` has run (catalog seeded).
2. The symbol universe is populated — `TaiwanStockInfo` has rows
   (`status` shows universe count). If not, enable + run it first; the
   per-symbol datasets need it to fan out.

All commands run inside the backend container:

```bash
cd /opt/finceptweb99
docker compose exec -T backend python -m finmind.scripts.<script> ...
```

## Step 1 — verify self-crawl matches FinMind (dry-run diff)

Row-count diff across all shippable datasets (no writes):

```bash
docker compose exec -T backend python -m finmind.scripts.dry_run_cutover --all
```

For the high-value price/chip datasets, do a per-column **value** diff
(needs a `compare_spec` in `mappings/_registry.py`):

```bash
docker compose exec -T backend python -m finmind.scripts.dry_run_cutover \
    --dataset TaiwanStockPrice --values --min-coverage 98
```

FAIL = the self-crawl side errored (must fix before flipping). WARN on a
row-count delta is expected (unit/None conventions differ; `mappings.py`
normalises them).

## Step 2 — cut over (the flip)

Preview first (dry-run is the default — nothing is written):

```bash
docker compose exec -T backend python -m finmind.scripts.cutover
```

Apply:

```bash
docker compose exec -T backend python -m finmind.scripts.cutover --commit
```

This flips `active_source` → the connector source and sets
`enabled = true` for all 24 covered datasets. It's idempotent — re-running
reports them "unchanged". Scope with `--source twse` or
`--dataset TaiwanStockPrice` to cut over incrementally (recommended:
start with `--source fred` and `--source tdcc`, the cheapest, then twse).

The AdminPage (`/admin` → FinMind) can also flip individual datasets via
the same gate; the CLI just does the whole covered set at once.

## Step 3 — history backfill (careful: hits TWSE hard)

Per dataset, resumable, month-by-month × symbol:

```bash
docker compose exec -T backend python -m finmind.scripts.deep_backfill \
    --dataset TaiwanStockPrice --years 5
```

**Rate limiting**: every request goes through the shared ~1 req/s Redis
token bucket (`data/tw/twse_connector`), so cron + backfill automatically
serialise against TWSE. Even so:

- **Run backfill off-peak** (roughly 00:00–07:00 Taipei) and **one
  dataset at a time** — a 5–10 year per-symbol backfill is thousands of
  requests. Watch `status --watch` in another shell.
- If TWSE starts erroring (403 / connection resets), **stop** and wait a
  day. There is no automatic 24h circuit breaker on the TWSE self-crawl
  path yet (only the FinMind connector has one) — treat repeated errors
  as a manual stop signal. Hardening this is a follow-up (see below).
- **Resume caveat**: `deep_backfill`'s "already done" check keys on
  `source="finmind"`, so after you flip a dataset its ledger rows are
  written under the new source and a re-run won't recognise prior
  `finmind` chunks — it re-fetches from scratch. Either backfill BEFORE
  flipping, or accept the re-fetch. (A `--rerun`/source-aware resume flag
  is a tracked follow-up.)

## Step 4 — daily cron

Install the committed FinceptWeb99 `/etc/cron.d` configuration:

```bash
install -m 0644 backend/finmind/deploy/finmind-cron \
  /etc/cron.d/finceptweb99-finmind-tw
install -m 0644 backend/finmind/deploy/finmind-tw-logrotate \
  /etc/logrotate.d/finceptweb99-finmind-tw
logrotate --debug /etc/logrotate.d/finceptweb99-finmind-tw
```

The production entries are fail-closed to `/opt/finceptweb99`, the
`finceptweb99` Compose project, and a single-flight lock. They pass
`--tw-only --skip-per-symbol`, so only Taiwan market-wide datasets are
eligible and per-symbol connectors are removed before ingest. `run_due`
is idempotent and freshness-gated, so a missed slot self-heals next tick.
It reports the fixed `finmind_tw_marketwide` job to Admin Ingest Health and
returns 0 for a clean/nothing-due sweep, 1 for failed chunks, or 2 for a
runner crash. The dedicated logrotate policy keeps 14 compressed daily logs,
rotates early at 50 MiB, and uses `copytruncate` because cron appends directly.

Do not add `--universe-from-tw-stock-info` to this cron until the
`tw_stock_info` universe has been repaired and bounded: the current
with-warrant feed can misclassify tens of thousands of six-character
rows as non-warrants. Per-symbol ingestion remains a manual, controlled
backfill meanwhile.

## Step 5 — verify

```bash
docker compose exec -T backend python -m finmind.scripts.status
```

Check: enabled count ↑, backfill progress rising, no recent errors. Then
spot-check a few rows in the AdminPage DB browser (`/admin` → DB) against
the TWSE website for the same date.

## Known follow-ups (not in this cutover)

- TWSE backfill hardening: jitter + explicit off-peak gate + 24h failure
  circuit breaker on the self-crawl path (today: manual off-peak + manual
  stop).
- `deep_backfill --rerun` / source-aware resume so a post-flip backfill
  resumes instead of re-fetching.
- The remaining ~35 TW datasets need new self-crawl connectors (R9).
