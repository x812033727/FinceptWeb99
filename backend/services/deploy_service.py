"""One-click admin redeploy.

Backend can't run docker-compose directly — it lives inside a container
with no docker socket and no docker CLI. Instead we touch a trigger file
on a shared volume; the host's `finceptweb99-deploy.path` systemd unit
notices the mtime change and runs `/usr/local/bin/finceptweb99-deploy.sh`
as root, which performs the full backup → fast-forward → build → migrate →
restart → verify SOP.

The deploy script writes phase progress to `deploy-status.json` in the
same shared volume; we expose that JSON via GET /api/admin/deploy/status
so the frontend can poll for progress without us needing any IPC.

Why a separate endpoint instead of reusing /api/admin/update? That one
is wired to settings.UPDATE_COMMAND for binary release upgrades; this
one is "pull HEAD of the dev branch and rebuild," semantically distinct
and worth a clean audit trail (actor + trigger_id surface in the status
JSON so concurrent admins don't confuse each other's runs).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

log = logging.getLogger(__name__)

HOST_DIR = Path("/host-trigger")
TRIGGER = HOST_DIR / "deploy-trigger"
META = HOST_DIR / "deploy-meta.json"
STATUS = HOST_DIR / "deploy-status.json"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def trigger_deploy(actor_id: str) -> dict[str, Any]:
    """Fire the host's deploy systemd unit by bumping the trigger file's
    mtime. Returns immediately with a `trigger_id` the frontend uses to
    distinguish this run from any concurrent in-flight or stale runs.

    Doesn't wait for the deploy to start — the systemd path watcher and
    oneshot service handle dispatch asynchronously. The frontend should
    poll `/api/admin/deploy/status` to observe phase progression.
    """
    if not HOST_DIR.exists():
        # /host-trigger isn't mounted — operator forgot the
        # docker-compose volume. Fail loud so admins notice rather
        # than silently swallowing every redeploy click.
        log.error("admin.deploy.host_dir_missing", extra={"path": str(HOST_DIR)})
        return {
            "status": "failed",
            "trigger_id": "",
            "message": (
                f"{HOST_DIR} not mounted. Add `./var:/host-trigger` to the "
                "backend service in docker-compose.yml and recreate."
            ),
        }

    trigger_id = str(uuid4())
    requested_at = _now_iso()

    META.write_text(
        json.dumps(
            {
                "actor": actor_id,
                "trigger_id": trigger_id,
                "requested_at": requested_at,
            }
        )
    )

    # Two-step touch: create if missing, then bump mtime explicitly.
    # `Path.touch()` only updates mtime when the file already exists on
    # some filesystems; `os.utime(path, None)` is the canonical way to
    # signal "now" without writing data, which is what the systemd
    # PathChanged trigger watches for.
    TRIGGER.touch(exist_ok=True)
    os.utime(TRIGGER, None)

    log.info(
        "admin.deploy.triggered",
        extra={"actor_id": actor_id, "trigger_id": trigger_id},
    )
    return {
        "status": "started",
        "trigger_id": trigger_id,
        "message": "deploy triggered; poll /api/admin/deploy/status for progress",
    }


async def read_deploy_status() -> dict[str, Any]:
    """Return the most recent status JSON written by the deploy script.

    Returns `{phase: "idle"}` when no deploy has ever run (status file
    doesn't exist), and `{phase: "unknown"}` on JSON parse error so the
    UI can surface "we lost track" without erroring out the polling
    query.
    """
    if not STATUS.exists():
        return {"phase": "idle"}
    try:
        return json.loads(STATUS.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("admin.deploy.status_unreadable", extra={"error": str(exc)})
        return {"phase": "unknown"}
