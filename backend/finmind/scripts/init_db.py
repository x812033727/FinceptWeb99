"""One-shot bootstrapper for the FinMind clone DB.

Usage (from `backend/`):

    python -m finmind.scripts.init_db          # alembic upgrade + seed catalog
    python -m finmind.scripts.init_db --check  # exit non-zero if not at head

Idempotent. Safe to re-run after every Phase-1 migration that fills in a
new `local_table` value in `dataset_catalog.py` — the seed step does an
UPSERT on `dataset_code`, so existing rows pick up new field values
without losing run telemetry (`last_ingest_at`, `last_error`, etc.).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

# When run as `python -m finmind.scripts.init_db` from `backend/`, the
# package import works. When invoked from a totally different cwd,
# inject backend/ on sys.path defensively.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from data.tw.finmind_datasets import find_dataset  # noqa: E402
from finmind.billing.quota import (  # noqa: E402
    FREE_PLAN_CODE,
    _FALLBACK_CALL_LIMIT,
    _FALLBACK_ROW_LIMIT,
)
from finmind.config import finmind_settings  # noqa: E402
from finmind.dataset_catalog import all_entries  # noqa: E402
from finmind.db.session import (  # noqa: E402
    FinmindAsyncSessionLocal,
    finmind_engine,
)
from finmind.models.billing import Plan  # noqa: E402
from finmind.models.dataset_source import DatasetSource  # noqa: E402

log = logging.getLogger("finmind.init_db")


def _alembic_config() -> Config:
    """Build an Alembic Config pointed at the FinMind subsystem's
    `alembic.ini`. Resolved relative to this file so the script works
    regardless of cwd."""
    ini_path = Path(__file__).resolve().parent.parent / "db" / "alembic.ini"
    if not ini_path.exists():
        raise FileNotFoundError(f"FinMind alembic.ini not found at {ini_path}")
    cfg = Config(str(ini_path))
    cfg.set_main_option("script_location", str(ini_path.parent / "migrations"))
    cfg.set_main_option(
        "sqlalchemy.url", finmind_settings.effective_database_url,
    )
    return cfg


def run_migrations() -> None:
    """Apply Alembic migrations up to head."""
    cfg = _alembic_config()
    log.info(
        "running alembic upgrade head (db=%s, schema=%s)",
        finmind_settings.effective_database_url.rsplit("@", 1)[-1],
        finmind_settings.schema or "<default>",
    )
    command.upgrade(cfg, "head")


async def check_head() -> int:
    """Return 0 when DB is at Alembic head, 1 otherwise. For CI / probes."""
    cfg = _alembic_config()
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(cfg)
    heads = set(script.get_heads())

    async with finmind_engine.connect() as conn:
        current = await conn.run_sync(
            lambda c: MigrationContext.configure(
                c,
                opts={
                    "version_table": "alembic_version_finmind",
                    "version_table_schema": finmind_settings.schema,
                },
            ).get_current_heads()
        )
    current_set = set(current)

    if current_set == heads:
        log.info("alembic at head: %s", sorted(heads))
        return 0
    log.error(
        "alembic NOT at head — current=%s expected=%s",
        sorted(current_set),
        sorted(heads),
    )
    return 1


async def seed_dataset_sources() -> tuple[int, int]:
    """UPSERT every catalog entry into `dataset_sources`. Returns
    (inserted_or_updated, total_in_catalog). Idempotent — existing rows'
    run telemetry (last_ingest_at, last_error, enabled) is preserved;
    only the routing fields are overwritten."""
    entries = all_entries()

    async with FinmindAsyncSessionLocal() as session:
        rows = []
        for category, entry in entries:
            spec = find_dataset(entry.dataset_code)
            assert spec is not None, (
                f"catalog references unknown dataset {entry.dataset_code} — "
                f"validation in dataset_catalog.py should have caught this"
            )
            rows.append(
                {
                    "dataset_code": entry.dataset_code,
                    "category": category,
                    "description_zh": spec.description,
                    "local_table": entry.local_table,
                    "per_symbol": spec.per_symbol,
                    "primary_source": entry.primary_source,
                    "fallback_source": entry.fallback_source,
                    "active_source": entry.primary_source,
                    "sponsor_tier": spec.sponsor_tier,
                    "ingest_freq": entry.ingest_freq,
                    "single_day": entry.single_day,
                }
            )

        # Dialect-aware UPSERT — Postgres in prod, SQLite in tests.
        dialect = session.bind.dialect.name
        if dialect == "postgresql":
            stmt = pg_insert(DatasetSource).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["dataset_code"],
                set_={
                    "category": stmt.excluded.category,
                    "description_zh": stmt.excluded.description_zh,
                    "local_table": stmt.excluded.local_table,
                    "per_symbol": stmt.excluded.per_symbol,
                    "primary_source": stmt.excluded.primary_source,
                    "fallback_source": stmt.excluded.fallback_source,
                    # Intentionally NOT updating active_source / enabled /
                    # last_ingest_at / last_ingest_rows / last_error — those
                    # are runtime state that a seed re-run mustn't clobber.
                    "sponsor_tier": stmt.excluded.sponsor_tier,
                    "ingest_freq": stmt.excluded.ingest_freq,
                    "single_day": stmt.excluded.single_day,
                },
            )
            await session.execute(stmt)
        elif dialect == "sqlite":
            stmt = sqlite_insert(DatasetSource).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["dataset_code"],
                set_={
                    "category": stmt.excluded.category,
                    "description_zh": stmt.excluded.description_zh,
                    "local_table": stmt.excluded.local_table,
                    "per_symbol": stmt.excluded.per_symbol,
                    "primary_source": stmt.excluded.primary_source,
                    "fallback_source": stmt.excluded.fallback_source,
                    "sponsor_tier": stmt.excluded.sponsor_tier,
                    "ingest_freq": stmt.excluded.ingest_freq,
                    "single_day": stmt.excluded.single_day,
                },
            )
            await session.execute(stmt)
        else:
            raise RuntimeError(
                f"unsupported dialect for FinMind seed: {dialect}"
            )

        await session.commit()
        total = (
            await session.execute(
                select(func.count()).select_from(DatasetSource)
            )
        ).scalar_one()

    return len(rows), int(total)


async def seed_default_free_plan() -> bool:
    """Insert the default 'free' plan row if absent. Returns True when
    inserted, False when it was already there.

    Idempotent on every init_db run. Existing rows (operator may have
    bumped the quota fields) are preserved — this only seeds a missing
    one. Operators wanting to reset to defaults can `DELETE` the row
    and re-run init_db."""
    async with FinmindAsyncSessionLocal() as session:
        existing = await session.get(Plan, FREE_PLAN_CODE)
        if existing is not None:
            return False
        session.add(
            Plan(
                code=FREE_PLAN_CODE,
                name="Free Tier",
                price_monthly=None,
                price_yearly=None,
                currency="TWD",
                allowed_datasets=None,
                quota_daily_calls=_FALLBACK_CALL_LIMIT,
                quota_daily_rows=_FALLBACK_ROW_LIMIT,
                enabled=True,
            )
        )
        await session.commit()
        return True


async def amain() -> int:
    parser = argparse.ArgumentParser(
        description="Initialize the FinMind clone DB (migrate + seed)."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only check Alembic is at head (exit 0/1); don't migrate or seed.",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="Run migrations but skip the dataset_sources seed.",
    )
    args = parser.parse_args()

    # `force=True` because alembic already configured the root logger when
    # `_alembic_config()` ran fileConfig — without it our format string is
    # silently ignored and our log lines disappear.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        force=True,
    )

    if args.check:
        return await check_head()

    # Migrations are sync — run in a worker thread so we don't block
    # the asyncio loop (and so Alembic's own asyncio.run() inside env.py
    # doesn't collide with ours).
    await asyncio.to_thread(run_migrations)

    if not args.no_seed:
        seeded, total = await seed_dataset_sources()
        free_inserted = await seed_default_free_plan()
        # `print` rather than `log.info` because alembic's env.py runs
        # `fileConfig()` on the alembic.ini, which silently overrides
        # the `basicConfig(force=True)` handlers above. A plain print
        # is loud, simple, and never goes missing.
        print(
            f"finmind.init_db: seeded {seeded} catalog rows; "
            f"total in dataset_sources = {total}"
        )
        print(
            "finmind.init_db: default 'free' plan "
            + ("inserted" if free_inserted else "already present")
        )

    await finmind_engine.dispose()
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    # Ensure the .env at repo root is picked up even when launched from
    # a sibling directory.
    os.chdir(_BACKEND_DIR)
    main()
