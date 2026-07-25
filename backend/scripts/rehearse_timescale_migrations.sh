#!/usr/bin/env bash
# backend/scripts/rehearse_timescale_migrations.sh
# Standing R6 rule, Timescale flavor: run the full migration chain
# against an ephemeral timescale container, then prove:
#   1. a compressed read returns identical rows (COMPRESSED-READ-OK)
#   2. the one known writer that can touch a compressed chunk
#      (tw_market_service.get_history's history-backfill upsert) still
#      succeeds against one (UPSERT-INTO-COMPRESSED-OK)
#   3. a downgrade -1 / upgrade head cycle preserves that exact state
#      (POST-CYCLE-READ-OK) — decompression survival is proven by a
#      real diff, not inferred from "the commands didn't error".
#
# Prod runs `timescale/timescaledb:latest-pg15` (docker-compose.yml).
# This rehearsal pins `2.26.3-pg16` — same TimescaleDB version family,
# one Postgres major ahead of prod. Noted here rather than glossed
# over; no compression-DDL behavior is known to differ across that gap.
set -euo pipefail

IMG=timescale/timescaledb:2.26.3-pg16

# Prefer 55433 (repo convention for this rehearsal); fall back to a
# free ephemeral port so a stale/parallel run doesn't just wedge here.
PORT=55433
if (echo >"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  echo "Port $PORT already in use — picking a free port instead" >&2
  PORT=$(python3 -c 'import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()')
fi

NAME=rehearse-ts-$$
docker run -d --rm --name "$NAME" -e POSTGRES_PASSWORD=x -e POSTGRES_DB=rehearse \
  -p "$PORT:5432" "$IMG" >/dev/null
trap 'docker stop "$NAME" >/dev/null' EXIT

# The official postgres/timescale entrypoint runs initdb, brings up a
# throwaway server to run init scripts, shuts it down, then starts the
# real server. `pg_isready` succeeds during that throwaway phase too, so
# waiting on it alone races the restart and asyncpg sees a connection
# reset mid-handshake. Wait for the "ready to accept connections" log
# line to appear twice (once per phase) instead.
for i in $(seq 1 60); do
  count=$(docker logs "$NAME" 2>&1 | grep -c "ready to accept connections" || true)
  [ "$count" -ge 2 ] && break
  sleep 1
done
docker exec "$NAME" pg_isready -U postgres -q

# config.Settings() is instantiated at import time by db/migrations/env.py
# (via `from config import settings`); DEBUG=true is required or the
# JWT_SECRET_KEY validator raises before alembic ever touches the DB.
export DEBUG=true
export DATABASE_URL="postgresql+asyncpg://postgres:x@127.0.0.1:${PORT}/rehearse"
alembic upgrade head

# Compressed-read equivalence: seed 200 days of bars, force-compress,
# diff a range read against the pre-compression answer, then simulate
# the one known writer that touches compressed chunks (see migration
# 0099's docstring: tw_market_service.get_history's history-backfill
# path, via upsert_ohlcv_bars_autosession's
# `INSERT ... ON CONFLICT (market, symbol, ts) DO UPDATE`) against an
# already-compressed row, and snapshot the resulting state into a
# regular (non-TEMP) table so it survives into the next psql session
# for the post-downgrade/upgrade-cycle diff below.
# NOTE: `source` is NOT NULL on ohlcv_daily (models/ohlcv_daily.py) —
# added to the brief's sample column list.
STEP1=$(docker exec -i "$NAME" psql -v ON_ERROR_STOP=1 -U postgres -d rehearse <<'SQL'
INSERT INTO ohlcv_daily (market, symbol, ts, open, high, low, close, volume, source)
SELECT 'TW', '2330', d::date, 100, 101, 99, 100.5, 1000, 'rehearsal'
FROM generate_series(now() - interval '200 days', now(), interval '1 day') d
ON CONFLICT DO NOTHING;

CREATE TEMP TABLE before_c AS
  SELECT * FROM ohlcv_daily WHERE symbol='2330' ORDER BY ts;

SELECT compress_chunk(c, true) FROM show_chunks('ohlcv_daily', older_than => interval '90 days') c;

SELECT CASE WHEN count(*) = 0 THEN 'COMPRESSED-READ-OK'
       ELSE 'COMPRESSED-READ-MISMATCH' END
FROM (
  (SELECT market,symbol,ts,open,high,low,close,volume FROM ohlcv_daily WHERE symbol='2330'
   EXCEPT
   SELECT market,symbol,ts,open,high,low,close,volume FROM before_c)
  UNION ALL
  (SELECT market,symbol,ts,open,high,low,close,volume FROM before_c
   EXCEPT
   SELECT market,symbol,ts,open,high,low,close,volume FROM ohlcv_daily WHERE symbol='2330')
) diff;

-- Simulate the history-backfill upsert landing on an already-compressed
-- row: same shape as upsert_ohlcv_bars_autosession's ON CONFLICT DO
-- UPDATE, targeting the oldest (guaranteed-compressed) seeded bar.
INSERT INTO ohlcv_daily (market, symbol, ts, open, high, low, close, volume, source)
SELECT market, symbol, ts, 999, 999, 999, 999, 99999, 'backfill'
FROM ohlcv_daily
WHERE symbol = '2330' AND ts < (now() - interval '95 days')::date
ORDER BY ts ASC
LIMIT 1
ON CONFLICT (market, symbol, ts) DO UPDATE SET
  open = excluded.open, high = excluded.high, low = excluded.low,
  close = excluded.close, volume = excluded.volume, source = excluded.source;

SELECT CASE WHEN count(*) = 1 THEN 'UPSERT-INTO-COMPRESSED-OK'
       ELSE 'UPSERT-INTO-COMPRESSED-MISMATCH' END
FROM ohlcv_daily
WHERE symbol = '2330' AND close = 999 AND source = 'backfill'
  AND ts < (now() - interval '95 days')::date;

-- Regular table (not TEMP) so it survives past this session — the
-- downgrade/upgrade cycle below runs as separate alembic invocations
-- against a fresh connection each time.
CREATE TABLE pre_cycle_snapshot AS
  SELECT * FROM ohlcv_daily WHERE symbol='2330' ORDER BY ts;
SQL
)
echo "$STEP1"
echo "$STEP1" | grep -q "COMPRESSED-READ-OK" || { echo "FAILED: compressed-read equivalence check did not pass" >&2; exit 1; }
echo "$STEP1" | grep -q "UPSERT-INTO-COMPRESSED-OK" || { echo "FAILED: upsert-into-compressed-chunk check did not pass" >&2; exit 1; }

alembic downgrade -1 && alembic upgrade head

# Decompression-survival proof: diff the post-cycle table against the
# snapshot taken right before the cycle (which already includes the
# backfill-simulated row) — this must come back empty.
STEP2=$(docker exec -i "$NAME" psql -v ON_ERROR_STOP=1 -U postgres -d rehearse <<'SQL'
SELECT CASE WHEN count(*) = 0 THEN 'POST-CYCLE-READ-OK'
       ELSE 'POST-CYCLE-READ-MISMATCH' END
FROM (
  (SELECT market,symbol,ts,open,high,low,close,volume FROM ohlcv_daily WHERE symbol='2330'
   EXCEPT
   SELECT market,symbol,ts,open,high,low,close,volume FROM pre_cycle_snapshot)
  UNION ALL
  (SELECT market,symbol,ts,open,high,low,close,volume FROM pre_cycle_snapshot
   EXCEPT
   SELECT market,symbol,ts,open,high,low,close,volume FROM ohlcv_daily WHERE symbol='2330')
) diff;

DROP TABLE pre_cycle_snapshot;
SQL
)
echo "$STEP2"
echo "$STEP2" | grep -q "POST-CYCLE-READ-OK" || { echo "FAILED: post-cycle decompression-survival check did not pass" >&2; exit 1; }

echo "REHEARSAL COMPLETE"
