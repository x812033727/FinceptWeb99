# Architecture notes

Decisions that look like duplication or missing abstractions but are
deliberate. Read before refactoring "DRY violations" away.

## Deliberate non-abstractions

### No generic `httpx.AsyncClient` wrapper

There are 9 places that create `httpx.AsyncClient` directly. They *look* like
they should share a wrapper, but the actual variation makes any single
abstraction either too narrow (forcing escape hatches) or too wide (no
benefit over raw httpx):

| Site | Special-cased | Why a wrapper would hurt |
|---|---|---|
| `ai/llm_router.py` | streaming POST + 120s timeout | needs raw httpx streaming API |
| `ai/tools/web.py` | `follow_redirects=True`, `max_redirects` | wrapper would have to expose every httpx flag |
| `data/tw/twse_connector.py` | upstream Redis token-bucket pacing | request flow is interleaved with rate-limit acquisition |
| `data/tw/mops_connector.py` | POST form + BeautifulSoup HTML parsing | not JSON; no shared parse step |
| `data/tw/finmind_connector.py` | API envelope `{status, data}`, daily quota counter | error semantics are domain-specific |
| `data/us/fred_connector.py` | bubble exceptions to caller | different error contract from `version_service` |
| `data/us/sp500_universe.py` | swallow exceptions, return cached list | different error contract from `fred_connector` |
| `services/version_service.py` | swallow exceptions, return None | different error contract again |

The cost of duplication is ~3 lines per site (`async with`, `get`,
`raise_for_status`). The cost of forcing them through one helper is bigger,
because any caller that needs a flag the helper doesn't expose has to escape
back to raw httpx anyway, and now you have two patterns instead of one.

If you hit five concrete callsites that *do* share the same shape, by all
means extract a helper for those — but resist the urge to make it generic.

### No generic `cached_fetch(key, fn, ttl)` helper

The fetch-or-cache pattern appears in ~6 files. The signatures vary by:

- **Parse**: `json.loads` for connectors, ORM `db.scalars(...)` for portfolio
  snapshots, dict-of-dataclass for analytics
- **Dump**: `json.dumps` for primitives, manual serialisation for ORM
- **Cache layers**: some services have in-process LRU + Redis + Postgres
  fallback (waterfall); a generic helper handling all three is its own
  mini-framework
- **Negative caching**: a few sites cache "not found" with shorter TTL;
  others don't

The two-line pattern (read-cache, on-miss fetch, write-cache) is small enough
that the abstraction overhead exceeds the savings. Documented here so the
next reviewer doesn't re-litigate.

## Deliberate small abstractions

### `data/us/sp500_universe.py`

Created during the 2026-04-25 cleanup. Before: `services/us_market_service.py`
and `tasks/us_market_refresh.py` each had a private `_sp500` list and a
private fetch function pointed at the same Wikipedia URL. The two caches
were independent — the daily scheduler wrote to one, the on-demand request
read from the other, so the scheduled refresh produced no visible effect.

After: a single module-level cache in `data/us/sp500_universe.py`. The
scheduler force-refreshes it; the service reads it. One source of truth.

If you find another data source that exhibits this same shape (cheap to
fetch, rarely changes, refreshed daily, read on-demand), extract a sibling
module under `data/{us,tw}/`. Don't generalise it into a "rarely-changing
list cache" framework — see the rule above.

## Module-level mutable state

In-process caches like `data/us/sp500_universe._cache` are intentional, not
lazy. They live for the lifetime of one uvicorn worker, which is exactly the
right TTL for "rarely-changing reference data that gets refreshed by a
scheduler". Moving them to Redis would add a network round-trip per request
for a list that changes a few times per quarter.

The trade-off is that with N uvicorn workers / k8s pods, the first request
to each pod misses cache and triggers a fetch. For S&P 500 that's one
Wikipedia GET per pod cold-start — fine. If you find yourself caching
something where the cold-start cost compounds (e.g. per-symbol), promote it
to Redis instead.

## Runtime topology

The FastAPI lifespan hook wires together four kinds of runtime work:

| Layer | Entry point | Responsibility |
|---|---|---|
| REST API | `backend/main.py` router includes | Auth, market data, portfolio, analytics, AI, discussion, admin, system |
| WebSocket | `api/websocket/*` | Auth-first client subscriptions, Redis Pub/Sub fan-out, alert pushes |
| Scheduler | `tasks/scheduler.py` | Quote polling, TW archives, sentiment scoring, portfolio snapshots, discussion jobs |
| Warmups / pumps | `main.py` lifespan tasks | TW symbol map + ETF yields, S&P 500 universe, Kraken ticker pump |

Keep these entry points separate. Request handlers should not start long-lived
jobs directly; use the scheduler for recurring work and the websocket manager
for fan-out.

## Background discussion jobs

Discussion has two distinct execution modes:

| Mode | Owner | Trigger | Notes |
|---|---|---|---|
| Manual rounds | Requesting user | `POST /api/discussion/sessions/{id}/round` | The route starts a background task and streams progress over SSE. If the browser disconnects, the producer task continues and persisted turns are visible on reload. |
| Daily auto-run | Each opted-in user | `tasks/auto_run_discussion.py` at 00:00 UTC | Reads `discussion_auto_run_configs`; each enabled user gets one `auto_run=True` discussion per UTC day, owned by themselves. |
| Outcome verification | Existing auto-run rows | `tasks/verify_discussion_outcome.py` at 08:30 UTC | Waits for a full 5-trading-day TW window, stores day1 open / day5 close snapshots, and stamps verdict. |

This is intentionally per-user now. Do not reintroduce a single
`ADMIN_EMAIL`-owned auto-run feed; that made successful jobs invisible to
other admins because sidebar reads are owner-scoped.

## Timestamp handling

SQLite can return timezone-aware `DateTime(timezone=True)` columns as naive
`datetime` objects. Repository code that compares stored quote timestamps to
`datetime.now(UTC)` must treat naive DB values as UTC, not local time. This is
why `services/ingest/repository.py` uses `_utc_timestamp()` for
`quote_snapshots` freshness checks and response timestamps.

Postgres/TimescaleDB remains the production target, but SQLite is the CI and
local-test backend. Keep repository helpers portable unless a test explicitly
marks a production-only path.

## Process and subprocess boundaries

Analytics CPU-heavy paths use a lazily-created `ProcessPoolExecutor`. The
lazy creation is deliberate: importing `services.analytics_service` should not
spawn multiprocessing pipes or worker state. This keeps unit-test collection
and lightweight imports stable, especially on Windows.

The `python_exec` AI tool is POSIX-hardened with `resource` limits when
available. `resource` is Unix-only, so the module must remain importable on
Windows and only attach `preexec_fn` when `os.name == "posix"`.

## Migration history

Migrations under `backend/db/migrations/versions/` form a linear chain:

| Rev | File | Purpose |
|-----|------|---------|
| 0001 | `0001_initial_schema.py` | users, api_keys, portfolios, holdings, transactions, watchlists |
| 0002 | `0002_add_price_alerts.py` | price_alerts table |
| 0003 | `0003_add_portfolio_snapshots.py` | portfolio_snapshots table |
| 0004 | `0004_portfolio_snapshots_hypertable.py` | PK reshape + TimescaleDB hypertable conversion |
| 0005 | `0005_llm_provider_keys.py` | admin-managed encrypted LLM provider keys |
| 0006 | `0006_persona_overrides.py` | per-persona provider/model routing overrides |
| 0007 | `0007_user_llm_provider_keys.py` | user-owned LLM provider keys |
| 0008 | `0008_llm_usage_events.py` | LLM token/cost usage ledger |
| 0009 | `0009_crypto_market.py` | crypto watchlist / market support |
| 0010 | `0010_market_provider_keys.py` | market-data provider key storage and validation |
| 0011 | `0011_ohlcv_daily.py` | archived daily OHLCV rows |
| 0012 | `0012_quote_snapshots.py` | periodic quote snapshots for outage fallback and retention |
| 0013 | `0013_fundamentals_snapshots.py` | archived fundamentals snapshots |
| 0014 | `0014_news_articles.py` | persisted market/news article rows |
| 0015 | `0015_discussions.py` | discussion sessions and turns, plus news sentiment columns |
| 0016 | `0016_system_task_configs.py` | admin-controlled provider/model routing for scheduled tasks |
| 0017 | `0017_runtime_settings.py` | runtime-configurable service settings |
| 0018 | `0018_discussion_verdict.py` | auto-run outcome fields and verification scheduling |
| 0019 | `0019_discussion_day5_close.py` | day5 close price snapshots for discussion verdict titles |
| 0020 | `0020_discussion_auto_run_configs.py` | per-user daily auto-run discussion configuration |

`portfolio_snapshots` PK is `(snapshot_date, id)` rather than `(id)` so it
satisfies TimescaleDB's "partitioning column must be in every UNIQUE index"
rule. Don't revert without rebuilding the hypertable.

## See also

- `CLAUDE.md` — top-level conventions, environment, test commands
- `docs/perf.md` — TimescaleDB hypertable benchmark methodology
