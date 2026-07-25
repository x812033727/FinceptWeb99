#!/usr/bin/env bash
# backend/scripts/rehearse_timescale_migrations.sh
# Standing R6 rule, Timescale flavor: run the full migration chain
# against an ephemeral timescale container, then prove a compressed
# read returns identical rows. Run from backend/.
set -euo pipefail

IMG=timescale/timescaledb:2.26.3-pg16
NAME=rehearse-ts-$$
docker run -d --rm --name "$NAME" -e POSTGRES_PASSWORD=x -e POSTGRES_DB=rehearse \
  -p 55433:5432 "$IMG" >/dev/null
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
export DATABASE_URL="postgresql+asyncpg://postgres:x@127.0.0.1:55433/rehearse"
alembic upgrade head

# Compressed-read equivalence: seed 200 days of bars, force-compress,
# and diff a range read against the pre-compression answer.
# NOTE: `source` is NOT NULL on ohlcv_daily (models/ohlcv_daily.py) —
# added to the brief's sample column list.
docker exec -i "$NAME" psql -U postgres -d rehearse <<'SQL'
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
SQL

alembic downgrade -1 && alembic upgrade head
echo "REHEARSAL COMPLETE"
