"""FinMind subsystem settings — separate Pydantic Settings instance.

Kept independent from the main `config.settings` so the FinMind clone can
later be extracted as a standalone service without rewiring env-var
plumbing. Reads from the same `.env` file (`extra="ignore"` means the
main app's vars don't error here, and vice versa).
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


# When sharing the main app's Postgres, all FinMind tables (and the
# alembic_version_finmind ledger) live in this dedicated schema so
# they never collide with the main app's `public` namespace. Future
# extraction back to a standalone DB is `pg_dump --schema=finmind`.
FINMIND_PG_SCHEMA = "finmind"


class FinmindSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Separate database — default points at the docker-compose
    # `postgres_finmind` service on port 5433, NOT the main app's 5432.
    FINMIND_DATABASE_URL: str = (
        "postgresql+asyncpg://finmind:password@localhost:5433/finmind_clone"
    )

    # When True, the FinMind subsystem ignores FINMIND_DATABASE_URL and
    # uses the main app's DATABASE_URL with a `finmind` Postgres schema
    # for isolation. Useful for small/managed deployments that can't run
    # a second Postgres container; preserves the future-extraction
    # property because every FinMind object lives under one schema and
    # `pg_dump --schema=finmind` ports it cleanly to a standalone DB.
    FINMIND_USE_MAIN_DB: bool = False

    # Sponsor tier = 1500 req/hour. Anonymous = 300/hr; registered free = 600/hr.
    # Keep ~100 req/hr headroom for the live cron when backfill is running.
    FINMIND_HOURLY_REQUEST_LIMIT: int = 1400

    # Default ingest source for newly-seeded dataset_sources rows.
    # Switch to 'twse' / 'tpex' / 'taifex' / 'mops' / 'tdcc' when the
    # FinMind subscription expires; flip per-row via the admin UI to
    # transition gradually rather than all-at-once.
    DEFAULT_PRIMARY_SOURCE: str = "finmind"

    # When True, the main app's lifespan startup hook auto-runs
    # `alembic upgrade head` + `seed_dataset_sources` + `seed_default_free_plan`
    # on the FinMind clone DB. Skipped silently if the DB isn't
    # reachable — operators with `postgres_finmind` not yet up still
    # boot the main app. Set FINMIND_AUTO_INIT=false when ops want
    # explicit control over migrations (e.g. multi-pod deploys where
    # only one pod should migrate).
    FINMIND_AUTO_INIT: bool = True

    DEBUG: bool = False

    @property
    def effective_database_url(self) -> str:
        """The URL the FinMind engine actually binds to.

        - FINMIND_USE_MAIN_DB=True  → main app's DATABASE_URL
        - else                      → FINMIND_DATABASE_URL (default)

        Late-imports `config.settings` so this module stays importable
        in contexts where the main settings haven't loaded yet (e.g.
        finmind tests with their own env)."""
        if not self.FINMIND_USE_MAIN_DB:
            return self.FINMIND_DATABASE_URL
        from config import settings as main_settings

        return main_settings.DATABASE_URL

    @property
    def schema(self) -> str | None:
        """Postgres schema for FinMind tables, or None for default
        public/isolated DB. SQLite (tests) gets None regardless because
        SQLite's schema concept is `ATTACH DATABASE` and we don't use
        it — tests already have isolated in-memory DBs."""
        if not self.FINMIND_USE_MAIN_DB:
            return None
        if self.effective_database_url.startswith("sqlite"):
            return None
        return FINMIND_PG_SCHEMA


finmind_settings = FinmindSettings()
