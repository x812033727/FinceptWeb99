# Contributing to FinceptWeb

Thanks for taking the time to contribute. This guide gets you from clone to
merged PR.

## Quick start

```bash
git clone https://github.com/<your-fork>/FinceptWeb.git
cd FinceptWeb
./scripts/dev-bootstrap.sh
```

The script provisions everything: `.env`, postgres + redis containers, Python
venv, frontend deps, migrations. Re-run it any time — it's idempotent.

After it finishes:

```bash
# terminal 1 — backend
cd backend && source .venv/bin/activate
uvicorn main:app --reload

# terminal 2 — frontend
cd frontend && npm run dev      # http://localhost:5173
```

## Project layout

See [CLAUDE.md](./CLAUDE.md) for the full architecture map. TL;DR:

- `backend/` — FastAPI + SQLAlchemy + Alembic. One package per domain under
  `api/`, business logic in `services/`, ORM in `models/`.
- `frontend/` — React 18 + Vite + TypeScript + TanStack Query + Zustand. One
  page per route under `src/pages/`.
- `scripts/` — operational scripts (bootstrap, pressure tests).
- `helm/`, `docker/`, `docker-compose.yml` — deployment.

## Running tests

```bash
# Backend (in-memory SQLite + AsyncMock Redis — no infra needed)
cd backend && pytest tests/ -v --asyncio-mode=auto

# Frontend (Vitest + jsdom)
cd frontend && npm test
```

CI runs the same commands on PRs — green locally usually means green in CI.

If `pytest` panics with `pyo3_runtime.PanicException`, your env is missing
`_cffi_backend`. The repo's `conftest.py` already side-steps this by swapping
`bcrypt` for `pbkdf2_sha256` in tests; if you still see it, run
`pip install --upgrade -r backend/requirements-dev.txt`.

## Lint and type checks

```bash
# Backend
cd backend && ruff check . --select E,W,F --ignore E501

# Frontend
cd frontend && npx tsc --noEmit
cd frontend && npm run lint
```

All four must be clean before opening a PR.

## Branching

- Default branch:  see GitHub repo home (it changes — don't hard-code `main`).
- Feature branch:  `feat/short-description` or `fix/short-description`.
- Don't push to the default branch directly — open a PR.

## Commit style

We squash-merge PRs, so individual commit messages don't survive — but
**PR titles do**, and they're the canonical history. Use Conventional Commits
for both:

```
feat(portfolio): add mean-variance optimiser
fix(ai): refund quota when stream yields zero tokens
chore(ci): bump ruff to 0.4.4
docs: clarify TWSE rate-limit fallback
test(tw): cover Redis token bucket exhaustion
```

PR descriptions should explain **why** more than **what** — the diff already
shows the what.

## Pull requests

1. Open a PR against the default branch.
2. CI must be green (6 jobs: backend test/lint, frontend test/lint/typecheck/build).
3. Link the issue: `Closes #123` in the PR body auto-closes on merge.
4. If your change touches the data path, update `CLAUDE.md`.
5. If your change touches public behaviour, add a test that fails before and
   passes after.

## Adding a backend dependency

Edit `backend/requirements.txt` (runtime) or `backend/requirements-dev.txt`
(test/dev). Pin to a specific version. Run the test suite locally to catch
breakage before pushing.

## Adding a database migration

```bash
cd backend
alembic revision -m "describe the change" --autogenerate
# Review the generated file before committing — autogenerate isn't perfect.
alembic upgrade head
```

Migrations must be reversible (`downgrade()` implemented) **unless** they
involve TimescaleDB hypertable conversions, which are documented as one-way
in `docs/perf.md`.

## Frontend conventions

- `api.ts` exports an axios instance with `baseURL: "/api"` — never include
  the `/api` prefix in path arguments.
- lightweight-charts: stay on **v4** API (`chart.addCandlestickSeries()`).
- Theme: use CSS variables (`hsl(var(--border))`); don't hard-code colors
  except for semantic data series (red/green for change direction, etc).
- Tests: prefer Testing Library queries (`getByRole`, `findByText`) over
  CSS selectors.

## Reporting issues

Use the issue tracker. Include:

- What you expected to happen
- What actually happened
- Steps to reproduce
- Your environment (OS, Python version, Node version)

For security issues, do **not** open a public issue. Email the maintainers
directly — see `SECURITY.md` (when available) or your org's contact.

## Code of conduct

Be kind, be specific, be helpful. Critique code, not people. Assume good
faith and ask questions before assuming malice.
