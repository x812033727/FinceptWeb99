"""FinMind clone subsystem — isolated package.

Lives in its own Postgres database (`FINMIND_DATABASE_URL`, default port
5433 via `postgres_finmind` docker-compose service) with its own Alembic
version table (`alembic_version_finmind`). Zero schema overlap with the
main `finceptweb` database — no shared models, no shared session, no
shared migrations.

The only thing imported across the boundary is the pure-data dataset
registry at `data.tw.finmind_datasets` (no DB ties, just dataclasses);
when this package is extracted into a standalone microservice, copy
that module in and the cut is clean.

See `CLAUDE.md` and recent design discussion for rationale (Path A:
keep separate forever; main app calls `/api/finmind/...` if needed).
"""
