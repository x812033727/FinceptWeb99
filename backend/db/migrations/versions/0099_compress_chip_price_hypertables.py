"""Timescale compression on the chip/price hypertables (spec Track 3a)

Revision ID: 0099
Revises: 0098

Rows are NEVER deleted — compression only. 90-day threshold keeps every
chunk the daily ingest walks (10-day lookback) and the discussion reads
(30-day history windows) uncompressed; only cold history compresses.

Deploy-critical: `ohlcv_daily`, `tw_institutional_daily`, and
`tw_margin_daily` are all pre-populated (real history, not empty
tables), so most of each already sits past the 90-day threshold at the
moment this migration runs. `add_compression_policy` schedules a
background job per table that starts working through that backlog
almost immediately after commit — a real, sustained compression I/O
burst against all three tables for some time post-deploy, not just a
future-dated policy sitting idle. Same caveat applies to migration
0100's `tw_stock_shareholding` conversion (see its docstring).

Requires TimescaleDB community license (prod: `timescale/timescaledb:
latest-pg15` per `docker-compose.yml`; rehearsed here against
`2.26.3-pg16` — same TimescaleDB feature set, one PG major version
ahead of prod. No compression-DDL behavior is known to differ across
that gap, but it's a real delta worth flagging rather than glossing
over). Both `upgrade()` and `downgrade()` are guarded by
`_has_timescaledb()` (same pattern as migrations 0004 / 0011 / 0021):
plain PostgreSQL — e.g. the e2e CI job's `postgres:16-alpine` service —
has neither the `timescaledb.compress` storage parameter nor
`add_compression_policy` / `remove_compression_policy` / `decompress_chunk`,
so without the guard both directions hard-fail there. Unit-test SQLite
never runs Alembic at all.

Table names are schema-qualified (`public.ohlcv_daily`, etc., including
inside `create_hypertable` / `add_compression_policy` / `show_chunks`
string arguments) rather than bare: prod has same-named tables in the
`finmind` schema (already compressed independently). `search_path`
resolves the bare names to the right table today, but qualifying them
removes that dependency entirely rather than leaving a regression the
rehearsal DB (which has no `finmind` schema) could never catch.

Known write against compressed chunks: `tw_market_service.get_history()`
can, on a user-triggered `/api/tw-market/history` request with `months`
up to 60, upsert bars older than the 90-day compression threshold via
`upsert_ohlcv_bars_autosession` (`services/ingest/repo/ohlcv.py`), which
issues `INSERT ... ON CONFLICT (market, symbol, ts) DO UPDATE`. Verified
(see `rehearse_timescale_migrations.sh`, `UPSERT-INTO-COMPRESSED-OK`)
to succeed against a compressed chunk on 2.26.3 — TimescaleDB allows the
upsert transparently. The accepted trade-off is occasional
decompress-on-write churn from these occasional history-backfill
requests; the daily scheduled ingest never triggers it since it only
ever walks the last 10 days.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0099"
down_revision: str | None = "0098"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "public.ohlcv_daily", "public.tw_institutional_daily", "public.tw_margin_daily",
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _has_timescaledb() -> bool:
    if not _is_postgres():
        return False
    return bool(op.get_bind().exec_driver_sql(
        "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"
    ).scalar())


def upgrade() -> None:
    # Plain Postgres (e2e CI's postgres:16-alpine) has no
    # timescaledb.compress storage param / add_compression_policy —
    # no-op there rather than hard-failing the whole migration chain.
    if not _has_timescaledb():
        return

    for t in _TABLES:
        op.execute(
            f"ALTER TABLE {t} SET ("
            f"timescaledb.compress, "
            f"timescaledb.compress_segmentby = 'market,symbol', "
            f"timescaledb.compress_orderby = 'ts DESC')"
        )
        op.execute(
            f"SELECT add_compression_policy('{t}', INTERVAL '90 days')"
        )


def downgrade() -> None:
    # Mirror the upgrade guard: remove_compression_policy /
    # decompress_chunk also don't exist on plain Postgres.
    if not _has_timescaledb():
        return

    for t in _TABLES:
        op.execute(
            f"SELECT remove_compression_policy('{t}', if_exists => true)"
        )
        op.execute(
            f"SELECT decompress_chunk(c, true) "
            f"FROM show_chunks('{t}') c"
        )
        op.execute(f"ALTER TABLE {t} SET (timescaledb.compress = false)")
