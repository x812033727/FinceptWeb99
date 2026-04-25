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

## Migration history

Migrations under `backend/db/migrations/versions/` form a linear chain:

| Rev | File | Purpose |
|-----|------|---------|
| 0001 | `0001_initial_schema.py` | users, api_keys, portfolios, holdings, transactions, watchlists |
| 0002 | `0002_add_price_alerts.py` | price_alerts table |
| 0003 | `0003_add_portfolio_snapshots.py` | portfolio_snapshots table |
| 0004 | `0004_portfolio_snapshots_hypertable.py` | PK reshape + TimescaleDB hypertable conversion |

`portfolio_snapshots` PK is `(snapshot_date, id)` rather than `(id)` so it
satisfies TimescaleDB's "partitioning column must be in every UNIQUE index"
rule. Don't revert without rebuilding the hypertable.

## See also

- `CLAUDE.md` — top-level conventions, environment, test commands
- `docs/perf.md` — TimescaleDB hypertable benchmark methodology
