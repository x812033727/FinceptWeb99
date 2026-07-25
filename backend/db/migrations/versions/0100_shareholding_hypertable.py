"""tw_stock_shareholding -> compressed hypertable (spec Track 3b).

433 MB and growing ~40 MB/month; weekly TDCC data compresses ~90% with
symbol segmenting. PK is (market, symbol, ts, bucket_id) and already
contains `ts`, so `create_hypertable` with `migrate_data => true` is
clean -- no PK surgery needed. Runs minutes of exclusive lock --
acceptable in the deploy window; only batch ingest touches this table.

Requires TimescaleDB (prod: `timescale/timescaledb:latest-pg15` per
`docker-compose.yml`; rehearsed here against `2.26.3-pg16` -- see
migration 0099's docstring for the version-gap note, which applies
identically here). Both `upgrade()` and `downgrade()` are guarded by
`_has_timescaledb()` (same pattern as migrations 0004 / 0011 / 0021 /
0099): plain PostgreSQL -- e.g. the e2e CI job's `postgres:16-alpine`
service -- has neither `create_hypertable`, the `timescaledb.compress`
storage parameter, nor `add_compression_policy` /
`remove_compression_policy` / `decompress_chunk`, so without the guard
both directions hard-fail the whole migration chain there.

Known writer against this table: `upsert_shareholdings`
(`services/ingest/repo/tw_chip.py`), via `_chunked_upsert`
(`services/ingest/repo/_common.py`), issues
`INSERT ... ON CONFLICT (market, symbol, ts, bucket_id) DO UPDATE SET
bucket_label = excluded.bucket_label, holders_count = excluded.holders_count,
shares_count = excluded.shares_count, shares_percent = excluded.shares_percent,
source = excluded.source`. Verified (see
`rehearse_timescale_migrations.sh`, `SHAREHOLDING-UPSERT-OK`) to succeed
against a compressed-era row on 2.26.3, same as 0099's
`UPSERT-INTO-COMPRESSED-OK` precedent for the price/chip tables.

NEVER deletes rows.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0100"
down_revision: str | None = "0099"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "tw_stock_shareholding"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _has_timescaledb() -> bool:
    if not _is_postgres():
        return False
    return bool(op.get_bind().exec_driver_sql(
        "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"
    ).scalar())


def upgrade() -> None:
    # Plain Postgres (e2e CI's postgres:16-alpine) has none of
    # create_hypertable / timescaledb.compress / add_compression_policy
    # -- no-op there rather than hard-failing the whole migration chain.
    if not _has_timescaledb():
        return

    # if_not_exists => true: `downgrade()` deliberately does NOT
    # un-hypertable the table (documented one-way conversion below), so
    # a downgrade -1 / upgrade head rehearsal cycle re-runs this against
    # a table that's already a hypertable. Without the flag that's a
    # hard error (matches migration 0004's portfolio_snapshots
    # precedent, the other migrate_data => true hypertable conversion
    # in this chain).
    op.execute(
        f"SELECT create_hypertable("
        f"'{_TABLE}', 'ts', "
        f"chunk_time_interval => INTERVAL '1 month', "
        f"migrate_data => true, "
        f"if_not_exists => true)"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} SET ("
        f"timescaledb.compress, "
        f"timescaledb.compress_segmentby = 'market,symbol', "
        f"timescaledb.compress_orderby = 'ts DESC, bucket_id')"
    )
    op.execute(
        f"SELECT add_compression_policy('{_TABLE}', INTERVAL '90 days')"
    )


def downgrade() -> None:
    # Mirror the upgrade guard: remove_compression_policy /
    # decompress_chunk also don't exist on plain Postgres.
    if not _has_timescaledb():
        return

    # Hypertable conversion is one-way in place; reversing it would mean
    # a full table rebuild (create plain table, copy all rows, swap).
    # Removing the policy + decompressing restores plain-table
    # read/write behaviour, which is all a rollback needs -- rows stay
    # exactly where they are, just as an uncompressed hypertable rather
    # than a plain table.
    op.execute(
        f"SELECT remove_compression_policy('{_TABLE}', if_exists => true)"
    )
    op.execute(
        f"SELECT decompress_chunk(c, true) "
        f"FROM show_chunks('{_TABLE}') c"
    )
    op.execute(f"ALTER TABLE {_TABLE} SET (timescaledb.compress = false)")
