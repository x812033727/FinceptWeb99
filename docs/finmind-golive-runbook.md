# FinMind clone go-live runbook (fincept99)

Status as of 2026-07-12. Covers what is live, and the decisions/steps
left for the operator. Companion to `finmind-replacement-blueprint.md`.

## What is LIVE now

- **All self-crawl code deployed** (origin/main, PRs W0/W5/W5b/W2/W3/W4/W2b
  + fix #40). Site healthy (HTTP 200). Migration `0022` applied → 5 crypto
  tables. `dataset_sources` = 90 rows.
- **Crypto pipeline running** (born self-sourced — Binance/CoinGecko, zero
  FinMind quota):
  - `crypto_universe` populated: 200 coins, ~102 Binance-mapped (`active`).
  - 5 crypto datasets `enabled=true`, `active_source` = binance/coingecko.
  - Initial ingest done: crypto_ohlcv (daily+hourly), funding, OI, asset_info.
  - **Cron** (`crontab -l`): hourly `run_due --crypto-universe-from-db`
    (due-detection covers 1h/8h/daily) + weekly `crypto_universe_refresh`.
  - Data is served through the clone's own API/billing surface. The main
    frontend's crypto charts still use the Kraken realtime path — wiring
    the UI to the clone's historical crypto is separate frontend work.
- **W0 usage instrumentation** deployed. It records per-dataset FinMind
  calls, but records nothing yet because no FinMind-sourced dataset is
  enabled on fincept99 (see below).

## Important context

The finmind clone on fincept99 was **dormant** before this go-live: its
`run_due` cron pointed at the *old* `/opt/finceptweb` site, all 85 TW
datasets were `enabled=false`, and the clone tables were empty. The
crypto go-live above is the only part that runs today.

## Decision 1 — activate TW Phase A (to collect W0 usage data)

W1 (prioritising the TW self-crawl cutover by real FinMind spend) needs
1–2 weeks of the W0 per-dataset usage counter. That only accumulates
while the TW datasets ingest **via FinMind**, which uses the 6000/hr
sponsor token.

To start it (uses FinMind quota):
1. Enable the TW datasets on `active_source='finmind'` (they already
   default to finmind; just flip `enabled`):
   ```sql
   UPDATE finmind.dataset_sources SET enabled=true
   WHERE category IN ('technical','chip','fundamental') AND active_source='finmind';
   ```
   (Start narrow; avoid the sponsor-tier/realtime ones first.)
2. Add a fincept99 TW cron (mirror the crypto one) — needs a TW symbol
   universe; `TaiwanStockInfo` must be enabled + ingested once first:
   ```
   */30 * * * * cd /opt/finceptweb99 && /usr/local/bin/docker-compose exec -T backend \
     python -m finmind.scripts.run_due --universe-from-tw-stock-info \
     >> /var/log/finmind99-tw.log 2>&1
   ```
3. After ~1–2 weeks: `python -m finmind.scripts.usage_report --days 14`
   → ranks datasets by FinMind spend = the W1 cutover order.

## Decision 2 — cut a TW dataset over to self-crawl

Wired self-crawl coverage today: **twse** (18 datasets), **mops** (4),
**taifex** (TaiwanFuturesDaily / TaiwanOptionDaily / TaiwanFuturesInstitutionalInvestors),
**tdcc** (TaiwanStockHoldingSharesPer), **fred** (GovernmentBondsYield /
CrudeOilPrices).

Per dataset, BEFORE flipping — reconcile values against FinMind (this is
why W0 built `--values`):
```
python -m finmind.scripts.dry_run_cutover --dataset <CODE> --source <SRC> --values
```
Resolve any WARN (coverage < 98% or per-column mismatch > 1%). For the
crawl sources note the documented conventions to check: taifex near-month
choice + `_FUT_NAME_TO_ID` codes + 買權/賣權 labels; fred verbose tenor
labels. Then flip:
```
PATCH /api/finmind/admin/datasets/<CODE>  {"active_source": "<SRC>"}
```
The scheduler routes the next tick through the crawl source; no code
change, no redeploy. `is_source_implemented` blocks flips to still-stubbed
sources (tpex).

## Optional — deep crypto history backfill

The cron keeps crypto fresh going forward, but the initial ingest only
pulled the recent window (~8 days daily, ~2 days hourly). For full chart
history, run a wide-range backfill (heavy Binance load — plan for an
off-peak window). Binance open-interest history is limited to ~30 days
regardless.

## Deploy gotchas (learned this go-live)

- **Rebuild the `migrate` image too** when a deploy adds a migration — it
  has its own image; rebuilding only backend/scheduler leaves migrate on
  old alembic scripts and it crash-loops ("Can't locate revision 0022"),
  which blocks backend startup. Safe deploy: `docker-compose build` (all
  code images incl. migrate) then `up -d`.
- Raw `text()` upserts must bind DATE/TIMESTAMP columns as python
  date/datetime objects, not ISO strings — Postgres rejects the str;
  SQLite (unit tests) does not, so it passes CI and fails in prod (fix #40).
