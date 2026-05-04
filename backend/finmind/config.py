"""FinMind subsystem settings — separate Pydantic Settings instance.

Kept independent from the main `config.settings` so the FinMind clone can
later be extracted as a standalone service without rewiring env-var
plumbing. Reads from the same `.env` file (`extra="ignore"` means the
main app's vars don't error here, and vice versa).
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


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


finmind_settings = FinmindSettings()
