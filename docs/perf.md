# Performance benchmarks

## TimescaleDB hypertable: `portfolio_snapshots`

Migration `0004_portfolio_snapshots_hypertable.py` converts `portfolio_snapshots`
from a regular Postgres table into a TimescaleDB hypertable partitioned by
`snapshot_date` with a 1-year chunk interval.

### Why

`services/portfolio_service.py` and the `/api/portfolio/{id}/performance`
endpoint issue range queries of the form:

```sql
SELECT snapshot_date, total_value_usd
FROM portfolio_snapshots
WHERE portfolio_id = :pid
  AND snapshot_date >= :cutoff
ORDER BY snapshot_date;
```

On a non-hypertable, the planner can use the `(portfolio_id, snapshot_date)`
btree, but the row-set still grows with the full retention window. With a
hypertable, the planner first prunes to the chunk(s) covering `:cutoff..NOW()`,
then uses the local btree — bounded scan size, predictable latency.

Q1 epic acceptance: **p95 < 50 ms** for the 1-year performance query at
10K portfolios × 365 snapshots ≈ 3.65M rows.

### Schema requirement satisfied by migration 0004

TimescaleDB requires the partitioning column (`snapshot_date`) be present in
every UNIQUE index on the table. Pre-0004 the PK was `(id)` only, which would
cause `create_hypertable` to fail with:

```
ERROR:  cannot create a unique index without the column "snapshot_date"
```

0004 reshapes the PK to `(snapshot_date, id)`. The existing
`uq_portfolio_snapshots_portfolio_id_snapshot_date` already includes
`snapshot_date`.

### Running the benchmark

The benchmark requires a Postgres + TimescaleDB instance with seeded data, so
it is run in staging — not in the local SQLite test suite.

```bash
# 1. bring up the stack (postgres + redis + backend)
docker compose up -d postgres redis

# 2. apply migrations
docker compose run --rm backend alembic upgrade head

# 3. seed 10K portfolios × 365 daily snapshots
docker compose exec postgres psql -U fincept -d finceptweb <<'SQL'
INSERT INTO portfolios (id, user_id, name, currency, created_at)
SELECT
    gen_random_uuid(),
    (SELECT id FROM users LIMIT 1),
    'bench_' || g,
    'USD',
    NOW()
FROM generate_series(1, 10000) g;

INSERT INTO portfolio_snapshots (id, portfolio_id, snapshot_date, total_value_usd, created_at)
SELECT
    gen_random_uuid(),
    p.id,
    CURRENT_DATE - d,
    100000 + random() * 50000,
    NOW()
FROM portfolios p, generate_series(0, 364) d
WHERE p.name LIKE 'bench_%';
SQL

# 4. measure p95
docker compose exec postgres psql -U fincept -d finceptweb -c "
EXPLAIN (ANALYZE, BUFFERS)
SELECT snapshot_date, total_value_usd
FROM portfolio_snapshots
WHERE portfolio_id = (SELECT id FROM portfolios WHERE name = 'bench_5000')
  AND snapshot_date >= CURRENT_DATE - INTERVAL '1 year'
ORDER BY snapshot_date;
"

# 5. repeat the EXPLAIN ANALYZE 100x via pgbench for a real distribution
docker compose exec postgres pgbench -n -T 60 -c 8 -f /tmp/snapshot_query.sql
```

`/tmp/snapshot_query.sql` template:

```sql
\set pid random_uuid
SELECT snapshot_date, total_value_usd
FROM portfolio_snapshots
WHERE portfolio_id = :pid::uuid
  AND snapshot_date >= CURRENT_DATE - INTERVAL '1 year'
ORDER BY snapshot_date;
```

### Pass criteria

- `EXPLAIN` plan includes `Custom Scan (ChunkAppend)` over the hypertable
- `pgbench` reports p95 latency < 50 ms at concurrency=8

### Known one-way operation

The `create_hypertable(...)` call is irreversible without data migration.
The Alembic `downgrade()` only reverts the PK shape — to fully roll back a
hypertable conversion in production, restore from a pre-0004 backup. Dev
environments can simply `docker compose down -v` and re-run migrations.
