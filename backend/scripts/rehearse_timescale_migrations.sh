#!/usr/bin/env bash
# backend/scripts/rehearse_timescale_migrations.sh
# Standing R6 rule, Timescale flavor: run the full migration chain
# against an ephemeral timescale container, then prove:
#   1. a compressed read returns identical rows (COMPRESSED-READ-OK)
#   2. the one known writer that can touch a compressed chunk
#      (tw_market_service.get_history's history-backfill upsert) still
#      succeeds against one (UPSERT-INTO-COMPRESSED-OK)
#   3. a downgrade -2 / upgrade head cycle (0100 -> 0099 -> 0098, then
#      back up) preserves that exact state for BOTH migrations
#      (POST-CYCLE-READ-OK for 0099's ohlcv_daily,
#      SHAREHOLDING-CYCLE-READ-OK for 0100's tw_stock_shareholding) —
#      decompression survival is proven by a real diff, not inferred
#      from "the commands didn't error". -2, not -1: with 0100 at head,
#      a -1 cycle would only ever revert 0100, leaving 0099's
#      reversibility untested and its marker vacuously green forever.
#   4. migration 0100's `create_hypertable(..., migrate_data => true)`
#      conversion of tw_stock_shareholding — seeded *before* 0100 runs,
#      so this is a genuine non-empty-table conversion, not an empty
#      hypertable being compressed after the fact — preserves every row
#      (SHAREHOLDING-READ-OK), and the shareholding writer
#      (`upsert_shareholdings`) still succeeds against a compressed-era
#      row (SHAREHOLDING-UPSERT-OK).
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

# FINDING (not in the brief), root-caused via `bash -x`: an
# `ERROR: chunk "_hyper_N_M_chunk" is already compressed` was observed
# on 2 of 6 rehearsal runs during this task, always in the
# tw_stock_shareholding block below — NOT inside migration 0100 itself
# (confirmed: every `alembic upgrade head` invocation traced with
# `bash -x` returned exit 0 and logged nothing past "Running upgrade
# 0099 -> 0100"; the ERROR line always appeared afterward, from this
# script's own explicit `compress_chunk` call). Cause: every seeded
# shareholding row is already >90 days old, so 0100's own
# `add_compression_policy` (added a few statements earlier, in the same
# migration) can start a background job compressing that backlog
# almost immediately — racing this script's own force-compress call for
# the same chunks. `ohlcv_daily`'s 0099 policy never hits this because
# it's added to an *empty* table; only once ohlcv_daily is seeded here,
# well after, does old data exist for it, by which point its policy's
# first sweep has long since found nothing to do. The per-chunk
# EXCEPTION handler at the shareholding compress step (below) tolerates
# only that specific error and re-raises anything else — plus a
# SHAREHOLDING-CHUNKS-COMPRESSED-OK assertion right after confirms
# compression actually happened (by that call or the policy's job,
# either is fine), since the SHAREHOLDING-READ-OK row-content diff alone
# can't tell compressed rows from uncompressed ones.
#
# Deliberately no retry wrapper around any `alembic upgrade` /
# `alembic downgrade` call below: prod's real deploy doesn't retry
# alembic, so a retry here would risk masking a genuine migration
# failure behind "it worked on attempt 2" — every failure above was
# confirmed (via `bash -x`) to already be past alembic returning exit 0,
# so alembic itself never needed retrying. The one place a transient
# race is expected — container startup — already has its own dedicated
# poll loop above (waiting for "ready to accept connections" x2 +
# `pg_isready`), which is the correct place for that kind of retry.
#
# Stop one short of head so tw_stock_shareholding can be seeded as a
# genuine plain table *before* 0100 converts it — otherwise the
# migrate_data => true conversion (the riskiest part of 0100: it runs
# under exclusive lock over ~2.64M rows in prod) would only ever be
# exercised against an empty table.
alembic upgrade 0099

STEP0=$(docker exec -i "$NAME" psql -v ON_ERROR_STOP=1 -U postgres -d rehearse <<'SQL'
-- 2 symbols x ~29 weekly rows spanning 200 days x 3 distinct bucket_ids
-- (source: models/tw_holdings_aggregates.py — PK is market,symbol,ts,
-- bucket_id; bucket_label/holders_count/shares_count/shares_percent
-- nullable, source NOT NULL).
INSERT INTO tw_stock_shareholding
  (market, symbol, ts, bucket_id, bucket_label, holders_count, shares_count, shares_percent, source)
SELECT 'TW', sym, d::date, b,
       'bucket_' || b, 1000 * b, 100000 * b, (b * 1.5)::numeric(8,4), 'rehearsal'
FROM (VALUES ('2330'), ('2317')) AS syms(sym)
CROSS JOIN generate_series(now() - interval '200 days', now(), interval '7 days') d
CROSS JOIN generate_series(1, 3) b
ON CONFLICT DO NOTHING;

-- Regular (non-TEMP) table: survives past this psql session into the
-- next `alembic upgrade head` invocation, which runs in a fresh
-- connection.
CREATE TABLE pre_migrate_snapshot_shareholding AS
  SELECT * FROM tw_stock_shareholding WHERE symbol IN ('2330', '2317')
  ORDER BY market, symbol, ts, bucket_id;
SQL
)
echo "$STEP0"

# Now run 0100: converts the just-seeded, non-empty tw_stock_shareholding
# into a compressed hypertable via migrate_data => true.
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

-- tw_stock_shareholding: force-compress the now-hypertable, then diff
-- against the pre-migration snapshot taken back in STEP0 — this proves
-- BOTH the migrate_data => true conversion AND the compression
-- preserved every row (not just compression, since that snapshot
-- predates 0100 entirely).
--
-- All rows seeded here are already >90 days old, so 0100's own
-- add_compression_policy (just added, a few statements ago) races
-- against this explicit compress_chunk call for the same chunks —
-- unlike the ohlcv_daily block above, where the 0099 policy was added
-- to an empty table and only sees old data once seeded well after.
-- Observed intermittently (2 of 6 rehearsal runs during this task):
-- `ERROR: chunk "_hyper_N_M_chunk" is already compressed` — confirmed
-- via `bash -x` that this is NOT 0100 failing (it always completed
-- with exit 0 in every reproduction) but this exact line losing a race
-- to the policy's own background job. Tolerate per-chunk: what matters
-- is every chunk ends up compressed by *someone*, which the
-- SHAREHOLDING-READ-OK diff below verifies regardless of who won.
DO $$
DECLARE
  c regclass;
BEGIN
  FOR c IN SELECT show_chunks('tw_stock_shareholding', older_than => interval '90 days') LOOP
    BEGIN
      PERFORM compress_chunk(c, true);
    EXCEPTION WHEN OTHERS THEN
      -- Only swallow the specific race with add_compression_policy's own
      -- background job; anything else (a genuine compression failure)
      -- must still fail loudly, not silently leave chunks uncompressed
      -- while SHAREHOLDING-READ-OK's row-content diff prints green anyway.
      IF SQLERRM NOT LIKE '%already compressed%' THEN
        RAISE;
      END IF;
      RAISE NOTICE 'compress_chunk(%) skipped: % (raced add_compression_policy''s own job)', c, SQLERRM;
    END;
  END LOOP;
END $$;

-- Positive assertion that compression actually happened (by this loop or
-- the policy's own job racing it) — the row-content diff below would
-- print SHAREHOLDING-READ-OK even if zero chunks got compressed, since
-- diffing content doesn't observe storage format. This is what actually
-- proves the compression half of "migrate_data => true conversion AND
-- compression preserved every row", not just the migrate_data half.
SELECT CASE WHEN count(*) FILTER (WHERE is_compressed) >= 1
             AND count(*) FILTER (
               WHERE NOT is_compressed AND range_end < now() - interval '90 days'
             ) = 0
       THEN 'SHAREHOLDING-CHUNKS-COMPRESSED-OK'
       ELSE 'SHAREHOLDING-CHUNKS-COMPRESSED-MISMATCH' END
FROM timescaledb_information.chunks
WHERE hypertable_name = 'tw_stock_shareholding';

SELECT CASE WHEN count(*) = 0 THEN 'SHAREHOLDING-READ-OK'
       ELSE 'SHAREHOLDING-READ-MISMATCH' END
FROM (
  (SELECT market,symbol,ts,bucket_id,bucket_label,holders_count,shares_count,shares_percent,source
   FROM tw_stock_shareholding WHERE symbol IN ('2330', '2317')
   EXCEPT
   SELECT market,symbol,ts,bucket_id,bucket_label,holders_count,shares_count,shares_percent,source
   FROM pre_migrate_snapshot_shareholding)
  UNION ALL
  (SELECT market,symbol,ts,bucket_id,bucket_label,holders_count,shares_count,shares_percent,source
   FROM pre_migrate_snapshot_shareholding
   EXCEPT
   SELECT market,symbol,ts,bucket_id,bucket_label,holders_count,shares_count,shares_percent,source
   FROM tw_stock_shareholding WHERE symbol IN ('2330', '2317'))
) diff;

-- Simulate the shareholding writer (`upsert_shareholdings` in
-- services/ingest/repo/tw_chip.py, via `_chunked_upsert` in
-- services/ingest/repo/_common.py) landing on an already-compressed
-- row: identical ON CONFLICT shape, targeting the oldest
-- (guaranteed-compressed) seeded row.
INSERT INTO tw_stock_shareholding
  (market, symbol, ts, bucket_id, bucket_label, holders_count, shares_count, shares_percent, source)
SELECT market, symbol, ts, bucket_id, 'updated', 999999, 8888888, 12.3456, 'rehearsal-upsert'
FROM tw_stock_shareholding
WHERE symbol IN ('2330', '2317') AND ts < (now() - interval '95 days')::date
ORDER BY ts ASC, bucket_id ASC
LIMIT 1
ON CONFLICT (market, symbol, ts, bucket_id) DO UPDATE SET
  bucket_label = excluded.bucket_label,
  holders_count = excluded.holders_count,
  shares_count = excluded.shares_count,
  shares_percent = excluded.shares_percent,
  source = excluded.source;

SELECT CASE WHEN count(*) = 1 THEN 'SHAREHOLDING-UPSERT-OK'
       ELSE 'SHAREHOLDING-UPSERT-MISMATCH' END
FROM tw_stock_shareholding
WHERE symbol IN ('2330', '2317') AND source = 'rehearsal-upsert' AND holders_count = 999999
  AND ts < (now() - interval '95 days')::date;

DROP TABLE pre_migrate_snapshot_shareholding;

CREATE TABLE pre_cycle_snapshot_shareholding AS
  SELECT * FROM tw_stock_shareholding WHERE symbol IN ('2330', '2317')
  ORDER BY market, symbol, ts, bucket_id;
SQL
)
echo "$STEP1"
echo "$STEP1" | grep -q "COMPRESSED-READ-OK" || { echo "FAILED: compressed-read equivalence check did not pass" >&2; exit 1; }
echo "$STEP1" | grep -q "UPSERT-INTO-COMPRESSED-OK" || { echo "FAILED: upsert-into-compressed-chunk check did not pass" >&2; exit 1; }
echo "$STEP1" | grep -q "SHAREHOLDING-CHUNKS-COMPRESSED-OK" || { echo "FAILED: shareholding chunk-compression assertion did not pass" >&2; exit 1; }
echo "$STEP1" | grep -q "SHAREHOLDING-READ-OK" || { echo "FAILED: shareholding migrate_data+compression equivalence check did not pass" >&2; exit 1; }
echo "$STEP1" | grep -q "SHAREHOLDING-UPSERT-OK" || { echo "FAILED: shareholding upsert-into-compressed-chunk check did not pass" >&2; exit 1; }

# -2, not -1: with 0100 at head, a plain `downgrade -1` only reverts
# 0100 — 0099's ohlcv/chip decompression-survival would then never be
# exercised again and POST-CYCLE-READ-OK would print green forever
# without actually testing anything (0099's tables wouldn't be touched
# by the cycle at all). `downgrade -2` walks 0100 -> 0099 -> 0098, then
# `upgrade head` walks back 0098 -> 0099 -> 0100, so both migrations'
# reversibility is proven on every rehearsal run. `if_not_exists => true`
# on 0100's `create_hypertable` call (see the migration's own comment)
# is what makes re-hypertabling tw_stock_shareholding on the way back up
# safe rather than a hard "already a hypertable" error.
alembic downgrade -2 && alembic upgrade head

# Decompression-survival proof: diff the post-cycle table against the
# snapshot taken right before the cycle (which already includes the
# backfill-simulated row) — this must come back empty. The deeper -2
# cycle above means both diffs below are genuine: POST-CYCLE-READ-OK
# for ohlcv_daily (0099's reversibility) and SHAREHOLDING-CYCLE-READ-OK
# for tw_stock_shareholding (0100's reversibility).
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

SELECT CASE WHEN count(*) = 0 THEN 'SHAREHOLDING-CYCLE-READ-OK'
       ELSE 'SHAREHOLDING-CYCLE-READ-MISMATCH' END
FROM (
  (SELECT market,symbol,ts,bucket_id,bucket_label,holders_count,shares_count,shares_percent,source
   FROM tw_stock_shareholding WHERE symbol IN ('2330', '2317')
   EXCEPT
   SELECT market,symbol,ts,bucket_id,bucket_label,holders_count,shares_count,shares_percent,source
   FROM pre_cycle_snapshot_shareholding)
  UNION ALL
  (SELECT market,symbol,ts,bucket_id,bucket_label,holders_count,shares_count,shares_percent,source
   FROM pre_cycle_snapshot_shareholding
   EXCEPT
   SELECT market,symbol,ts,bucket_id,bucket_label,holders_count,shares_count,shares_percent,source
   FROM tw_stock_shareholding WHERE symbol IN ('2330', '2317'))
) diff;

DROP TABLE pre_cycle_snapshot;
DROP TABLE pre_cycle_snapshot_shareholding;
SQL
)
echo "$STEP2"
echo "$STEP2" | grep -q "POST-CYCLE-READ-OK" || { echo "FAILED: post-cycle decompression-survival check did not pass" >&2; exit 1; }
echo "$STEP2" | grep -q "SHAREHOLDING-CYCLE-READ-OK" || { echo "FAILED: shareholding post-cycle decompression-survival check did not pass" >&2; exit 1; }

echo "REHEARSAL COMPLETE"
