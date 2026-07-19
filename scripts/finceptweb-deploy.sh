#!/usr/bin/env bash
# Fail-closed deployment runner for the FinceptWeb99 production stack.
#
# The systemd unit supplies every target-specific value. Keeping those values
# out of defaults is deliberate: invoking this script without the dedicated
# unit must stop before git, backup, migration, or compose can mutate anything.
set -Eeuo pipefail

required=(REPO BRANCH COMPOSE_PROJECT_NAME HEALTH_URL INSTALL_PATH EXPECTED_REMOTE)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "finceptweb deploy: required environment variable $name is missing" >&2
    exit 64
  fi
done

if [[ "$REPO" != /* || "$INSTALL_PATH" != /* ]]; then
  echo "finceptweb deploy: REPO and INSTALL_PATH must be absolute paths" >&2
  exit 64
fi
if [[ ! "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]]; then
  echo "finceptweb deploy: invalid BRANCH" >&2
  exit 64
fi
if [[ ! "$COMPOSE_PROJECT_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
  echo "finceptweb deploy: invalid COMPOSE_PROJECT_NAME" >&2
  exit 64
fi
if [[ ! "$HEALTH_URL" =~ ^https?:// ]]; then
  echo "finceptweb deploy: HEALTH_URL must be an HTTP(S) URL" >&2
  exit 64
fi
if [[ ! -d "$REPO/.git" || ! -d "$REPO/var" ]]; then
  echo "finceptweb deploy: REPO must contain .git and var directories" >&2
  exit 64
fi

VAR="$REPO/var"
STATUS="$VAR/deploy-status.json"
META="$VAR/deploy-meta.json"
LOG="$VAR/deploy.log"
LOCK="$VAR/deploy.lock"
COMPOSE=(docker-compose -p "$COMPOSE_PROJECT_NAME")
APP_SERVICES=(backend scheduler db-backup frontend nginx)

# A rejected second trigger must not overwrite the in-flight run's status.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "finceptweb deploy: another deploy is already in flight" >&2
  exit 75
fi

ACTOR="$(jq -r '.actor // ""' "$META" 2>/dev/null || true)"
TRIGGER_ID="$(jq -r '.trigger_id // ""' "$META" 2>/dev/null || true)"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BEFORE_SHA="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || true)"
LAST_PHASE=starting

write_status() {
  local phase=$1
  local error=${2:-}
  local finished_at=""
  local after_sha=""
  local log_tail="[]"

  if [[ "$phase" == completed || "$phase" == failed ]]; then
    finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    after_sha="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || true)"
    if [[ -f "$LOG" ]]; then
      log_tail="$(tail -n 20 "$LOG" | jq -R . | jq -s .)"
    fi
  fi

  jq -n \
    --arg phase "$phase" \
    --arg started_at "$STARTED_AT" \
    --arg finished_at "$finished_at" \
    --arg before_sha "$BEFORE_SHA" \
    --arg after_sha "$after_sha" \
    --arg branch "$BRANCH" \
    --arg actor "$ACTOR" \
    --arg trigger_id "$TRIGGER_ID" \
    --arg error "$error" \
    --argjson log_tail "$log_tail" \
    '{phase:$phase, started_at:$started_at,
      finished_at:(if $finished_at == "" then null else $finished_at end),
      before_sha:(if $before_sha == "" then null else $before_sha end),
      after_sha:(if $after_sha == "" then null else $after_sha end),
      branch:$branch, actor:$actor, trigger_id:$trigger_id,
      error:(if $error == "" then null else $error end), log_tail:$log_tail}' \
    >"$STATUS.tmp"
  mv "$STATUS.tmp" "$STATUS"
}

on_error() {
  local rc=$?
  trap - ERR
  write_status failed "$LAST_PHASE failed (exit $rc)"
  exit "$rc"
}
trap on_error ERR

log() {
  printf '%s\n' "$*" | tee -a "$LOG"
}

fail() {
  log "ERROR: $*"
  return 1
}

set_phase() {
  LAST_PHASE=$1
  write_status "$LAST_PHASE"
  log "--- phase=$LAST_PHASE ---"
}

preflight() {
  local top_level remote branch
  top_level="$(git -C "$REPO" rev-parse --show-toplevel)"
  [[ "$(realpath "$top_level")" == "$(realpath "$REPO")" ]] ||
    fail "git top-level does not match REPO"

  remote="$(git -C "$REPO" remote get-url origin)"
  [[ "$remote" == "$EXPECTED_REMOTE" ]] ||
    fail "origin remote does not match EXPECTED_REMOTE"

  branch="$(git -C "$REPO" symbolic-ref --quiet --short HEAD)"
  [[ "$branch" == "$BRANCH" ]] || fail "checked-out branch is $branch, expected $BRANCH"

  # Untracked operational files are intentionally allowed. Tracked or staged
  # changes make a fast-forward deployment ambiguous and are rejected.
  git -C "$REPO" diff --quiet || fail "tracked worktree changes detected"
  git -C "$REPO" diff --cached --quiet || fail "staged changes detected"
}

create_and_verify_backup() {
  local backup_jwt
  # Older checked-out compose files did not pass JWT_SECRET_KEY to db-backup,
  # although importing the application settings requires it. Resolve the
  # already-interpolated backend value without printing it, then forward only
  # the variable name to the one-shot container. New compose revisions also
  # pass it directly, so this remains backward-compatible for the first
  # hardened deployment.
  backup_jwt="$("${COMPOSE[@]}" config --format json |
    jq -er '.services.backend.environment.JWT_SECRET_KEY | select(length > 0)')"
  export JWT_SECRET_KEY="$backup_jwt"

  "${COMPOSE[@]}" run --rm --no-deps -e JWT_SECRET_KEY db-backup \
    python -m scripts.database_backup --directory /backups 2>&1 | tee -a "$LOG"

  "${COMPOSE[@]}" run --rm --no-deps db-backup sh -ec '
    latest="$(ls -1t /backups/fincept_*.dump | head -n1)"
    test -n "$latest" && test -s "$latest"
    manifest="${latest%.dump}.json"
    test -s "$manifest"
    python -c '\''import hashlib,json,pathlib,sys; p=pathlib.Path(sys.argv[1]); m=json.loads(p.with_suffix(".json").read_text()); assert m["archive"] == p.name; assert m["bytes"] == p.stat().st_size and m["bytes"] > 0; assert m["toc_entries"] > 0; assert m["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()'\'' "$latest"
    pg_restore --list "$latest" >/dev/null
  ' 2>&1 | tee -a "$LOG"

  unset JWT_SECRET_KEY backup_jwt
}

reset_stale_jobs() {
  "${COMPOSE[@]}" exec -T postgres sh -ec '
    psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
      "UPDATE finmind.backfill_progress
       SET status='\''pending'\'', error_message='\''deploy interrupted'\''
       WHERE status='\''running'\''
         AND started_at < NOW() - interval '\''5 minutes'\'';"
  ' 2>&1 | tee -a "$LOG"
}

run_migrations() {
  "${COMPOSE[@]}" run --rm --no-deps migrate sh -ec \
    'alembic upgrade head && python -m finmind.scripts.init_db' 2>&1 | tee -a "$LOG"
}

containers_ready() {
  local service container state health
  for service in "${APP_SERVICES[@]}"; do
    container="$("${COMPOSE[@]}" ps -q "$service")"
    [[ -n "$container" ]] || return 1
    state="$(docker inspect --format '{{.State.Status}}' "$container")"
    [[ "$state" == running ]] || return 1
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container")"
    [[ "$health" == healthy ]] || return 1
  done
}

verify_deployment() {
  local attempt
  for attempt in {1..60}; do
    if curl -sSf -o /dev/null "$HEALTH_URL" && containers_ready; then
      return 0
    fi
    sleep 3
  done
  fail "health or container verification timed out"
}

sync_installed_script() {
  local source temp
  source="$REPO/scripts/finceptweb-deploy.sh"
  temp="${INSTALL_PATH}.tmp.$$"
  bash -n "$source"
  install -m 0755 "$source" "$temp"
  mv -f "$temp" "$INSTALL_PATH"
}

: >"$LOG"
log "=== FinceptWeb99 deploy started $STARTED_AT actor=$ACTOR trigger_id=$TRIGGER_ID branch=$BRANCH ==="
write_status starting

# No backup, fetch, build, migration, or compose mutation may occur before all
# target identity and worktree checks pass.
preflight

set_phase backing_up
create_and_verify_backup

set_phase pulling
git -C "$REPO" fetch --prune origin "$BRANCH" 2>&1 | tee -a "$LOG"
git -C "$REPO" merge --ff-only "origin/$BRANCH" 2>&1 | tee -a "$LOG"

set_phase building
"${COMPOSE[@]}" build backend frontend migrate scheduler db-backup 2>&1 | tee -a "$LOG"

# Build before stopping request and scheduler processes to keep downtime short.
set_phase pausing
"${COMPOSE[@]}" stop backend scheduler 2>&1 | tee -a "$LOG"

set_phase reset_stale
reset_stale_jobs

set_phase migrating
run_migrations

set_phase restarting
"${COMPOSE[@]}" up -d --no-deps backend scheduler frontend db-backup 2>&1 | tee -a "$LOG"

set_phase nginx
"${COMPOSE[@]}" up -d --no-deps nginx 2>&1 | tee -a "$LOG"

set_phase verifying
verify_deployment
sync_installed_script

LAST_PHASE=completed
write_status completed
log "=== FinceptWeb99 deploy completed $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
