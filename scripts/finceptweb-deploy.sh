#!/usr/bin/env bash
# Triggered by /etc/systemd/system/finceptweb-deploy.path watching
# /opt/finceptweb/var/deploy-trigger. Runs the full FinceptWeb redeploy
# SOP and writes status JSON the backend exposes via
# GET /api/admin/deploy/status.
#
# Canonical install path: /usr/local/bin/finceptweb-deploy.sh. The
# systemd unit invokes that copy, not this one — sync after edits:
#     sudo install -m 0755 scripts/finceptweb-deploy.sh \
#         /usr/local/bin/finceptweb-deploy.sh
# This file in the repo is the source of truth; drift between the two
# means deploy behaviour and reviewable history disagree.
set -Eeuo pipefail

REPO=/opt/finceptweb
VAR=$REPO/var
STATUS=$VAR/deploy-status.json
META=$VAR/deploy-meta.json
LOG=$VAR/deploy.log
LOCK=$VAR/deploy.lock
BRANCH="${BRANCH:-claude/create-terminal-documentation-iBmfW}"

# Single-flight lock so a second trigger landing mid-deploy doesn't
# stomp the first. flock -n returns immediately when the lock is held;
# we report failed and exit, leaving the in-flight run undisturbed.
exec 9>"$LOCK"
if ! flock -n 9; then
  printf '{"phase":"failed","error":"another deploy in flight","finished_at":"%s"}' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$STATUS"
  exit 1
fi

ACTOR="$(jq -r '.actor // ""' "$META" 2>/dev/null || echo "")"
TRIGGER_ID="$(jq -r '.trigger_id // ""' "$META" 2>/dev/null || echo "")"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BEFORE_SHA="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo "")"
LAST_PHASE=starting

write_status() {
  local phase=$1
  local err=${2:-}
  local finished_at="null"
  local after_sha="null"
  local log_tail="[]"
  if [[ "$phase" == "completed" || "$phase" == "failed" ]]; then
    finished_at="\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
    after_sha="\"$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo "")\""
    if [[ -f "$LOG" ]]; then
      log_tail="$(tail -n 20 "$LOG" | jq -R . | jq -s .)"
    fi
  fi
  jq -n \
    --arg phase "$phase" \
    --arg started_at "$STARTED_AT" \
    --argjson finished_at "$finished_at" \
    --arg before_sha "$BEFORE_SHA" \
    --argjson after_sha "$after_sha" \
    --arg branch "$BRANCH" \
    --arg actor "$ACTOR" \
    --arg trigger_id "$TRIGGER_ID" \
    --arg error "$err" \
    --argjson log_tail "$log_tail" \
    '{phase:$phase, started_at:$started_at, finished_at:$finished_at,
      before_sha:$before_sha, after_sha:$after_sha, branch:$branch,
      actor:$actor, trigger_id:$trigger_id,
      error:(if $error == "" then null else $error end),
      log_tail:$log_tail}' > "$STATUS.tmp"
  mv "$STATUS.tmp" "$STATUS"
}

on_err() {
  local rc=$?
  write_status failed "$LAST_PHASE failed (exit $rc)"
  exit "$rc"
}
trap on_err ERR

: > "$LOG"
echo "=== deploy started $(date -u +%Y-%m-%dT%H:%M:%SZ) actor=$ACTOR trigger_id=$TRIGGER_ID branch=$BRANCH ===" | tee -a "$LOG"

LAST_PHASE=starting    ; write_status starting
LAST_PHASE=pulling     ; write_status pulling
cd "$REPO" && git pull --ff-only origin "$BRANCH" 2>&1 | tee -a "$LOG"

# Self-heal drift between the systemd-invoked copy at
# /usr/local/bin/finceptweb-deploy.sh and the in-repo source of truth.
# Without this, edits to scripts/finceptweb-deploy.sh only land in
# /usr/local/bin/ when someone remembers to run `sudo install` — and
# "forgot to install" silently downgrades the next deploy. Concrete
# regression we hit on 2026-05-08: the in-repo script had been updated
# to `build backend frontend migrate`, but the installed copy still
# said `build backend frontend`, so the migrate image stayed cached,
# alembic ran against stale revisions, and DB sat at 0054 while
# backend code expected 0055 → UndefinedColumn on /discussion.
INSTALL_PATH=/usr/local/bin/finceptweb-deploy.sh
REPO_SCRIPT=$REPO/scripts/finceptweb-deploy.sh
if [[ "${FINCEPTWEB_DEPLOY_REEXECED:-}" != "1" ]] \
   && ! cmp -s "$REPO_SCRIPT" "$INSTALL_PATH"; then
  echo "deploy script drift detected; installing $REPO_SCRIPT and re-executing" | tee -a "$LOG"
  install -m 0755 "$REPO_SCRIPT" "$INSTALL_PATH"
  export FINCEPTWEB_DEPLOY_REEXECED=1
  exec "$INSTALL_PATH" "$@"
fi

LAST_PHASE=pausing     ; write_status pausing
docker-compose stop backend 2>&1 | tee -a "$LOG"

LAST_PHASE=reset_stale ; write_status reset_stale
docker exec finceptweb-postgres-1 psql -U fincept -d finceptweb -c \
  "UPDATE finmind.backfill_progress SET status='pending', error_message='deploy interrupted' WHERE status='running' AND started_at < NOW() - interval '5 minutes';" \
  2>&1 | tee -a "$LOG"

LAST_PHASE=building    ; write_status building
# `migrate` must be in the build list. It uses the same Dockerfile as
# backend (`build: ./backend`) but compose treats them as separate
# images — leaving it out means the migrate container ran with the
# pre-pull image and never sees new alembic revisions, so the DB
# stays N versions behind even though backend ships the new schema.
# Symptom: backend boots fine but errors on UndefinedColumn for
# anything added in the missed migrations.
docker-compose build backend frontend migrate 2>&1 | tee -a "$LOG"

LAST_PHASE=restarting  ; write_status restarting
# `up -d backend` recreates `migrate` via `depends_on:
# service_completed_successfully` — with the freshly-built image this
# time — so `alembic upgrade head` runs against the new revisions
# before backend starts.
docker-compose up -d backend frontend 2>&1 | tee -a "$LOG"

LAST_PHASE=nginx       ; write_status nginx
docker-compose restart nginx 2>&1 | tee -a "$LOG"

LAST_PHASE=verifying   ; write_status verifying
timeout 180 bash -c 'until curl -sSf -o /dev/null http://127.0.0.1:8080/api/health; do sleep 3; done' 2>&1 | tee -a "$LOG"

LAST_PHASE=completed   ; write_status completed
echo "=== deploy completed $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" | tee -a "$LOG"
