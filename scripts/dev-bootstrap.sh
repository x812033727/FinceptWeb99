#!/usr/bin/env bash
# One-shot dev bootstrap for FinceptWeb.
#
# Brings a fresh clone to a state where you can run the backend and frontend
# locally:
#   - sanity-checks required tools
#   - copies .env.example → .env when missing (and rolls a JWT secret)
#   - starts postgres + redis via docker compose
#   - creates a Python venv, installs backend deps, applies migrations
#   - installs frontend deps
#
# Usage
#   ./scripts/dev-bootstrap.sh
#
# Re-running is safe: existing .env / venv / deps are kept.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

log()  { printf "\033[1;36m[bootstrap]\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m[ok]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m %s\n" "$*"; }
die()  { printf "\033[1;31m[fail]\033[0m %s\n" "$*" >&2; exit 1; }

# ── 1. tool check ─────────────────────────────────────────────────
log "Checking required tools…"
command -v docker  >/dev/null || die "docker not found — install Docker Desktop / Engine first"
command -v python3 >/dev/null || die "python3 not found"
command -v node    >/dev/null || die "node not found — install Node 20+"

py_major=$(python3 -c 'import sys; print(sys.version_info[0])')
py_minor=$(python3 -c 'import sys; print(sys.version_info[1])')
if [ "$py_major" -lt 3 ] || { [ "$py_major" -eq 3 ] && [ "$py_minor" -lt 11 ]; }; then
  die "Python 3.11+ required (found $py_major.$py_minor)"
fi

node_major=$(node -p 'process.versions.node.split(".")[0]')
[ "$node_major" -ge 20 ] || die "Node 20+ required (found $node_major)"
ok "Tools OK"

# ── 2. .env files ─────────────────────────────────────────────────
if [ ! -f .env ]; then
  log "Creating .env from .env.example"
  cp .env.example .env
  # Roll a real JWT secret (32 hex bytes = 64 chars).
  jwt=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  # macOS sed needs an extension arg to -i; GNU sed doesn't. Use a portable form.
  python3 - "$jwt" <<'PY'
import pathlib, sys, re
secret = sys.argv[1]
p = pathlib.Path(".env")
p.write_text(re.sub(
    r"^JWT_SECRET_KEY=.*$",
    f"JWT_SECRET_KEY={secret}",
    p.read_text(),
    flags=re.M,
))
PY
  ok ".env created with a fresh JWT_SECRET_KEY"
else
  ok ".env already exists — leaving it alone"
fi

if [ ! -f frontend/.env ] && [ -f frontend/.env.example ]; then
  cp frontend/.env.example frontend/.env
  ok "frontend/.env created from frontend/.env.example"
fi

# ── 3. infra (postgres + redis) ───────────────────────────────────
log "Starting postgres + redis (docker compose)…"
docker compose up -d postgres redis

log "Waiting for postgres to be healthy…"
for _ in $(seq 1 30); do
  if docker compose ps --format json postgres 2>/dev/null | grep -q '"Health":"healthy"'; then
    ok "postgres healthy"
    break
  fi
  sleep 2
done

# ── 4. backend deps + migrations ──────────────────────────────────
log "Setting up backend venv…"
if [ ! -d backend/.venv ]; then
  python3 -m venv backend/.venv
  ok "Created backend/.venv"
fi

# shellcheck disable=SC1091
source backend/.venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r backend/requirements.txt -r backend/requirements-dev.txt
ok "Backend deps installed"

log "Applying migrations…"
( cd backend && alembic upgrade head )
ok "Migrations applied"
deactivate

# ── 5. frontend deps ──────────────────────────────────────────────
log "Installing frontend deps…"
( cd frontend && npm ci --no-audit --no-fund )
ok "Frontend deps installed"

# ── 6. summary ────────────────────────────────────────────────────
cat <<'EOF'

============================================================
  Bootstrap complete. Next steps:

  Backend:
    cd backend
    source .venv/bin/activate
    uvicorn main:app --reload

  Frontend (in another terminal):
    cd frontend
    npm run dev

  Tests:
    cd backend && pytest tests/ --asyncio-mode=auto
    cd frontend && npm test

  Stop infra when done:
    docker compose down
============================================================
EOF
