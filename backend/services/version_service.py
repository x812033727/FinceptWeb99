"""GitHub release polling + admin-triggered update.

Flow:
    scheduler (every UPDATE_CHECK_INTERVAL_HOURS) ──► refresh_release_cache()
        └─ fetch_latest_release() ──► Redis (TTL > scheduler interval)

    GET /api/system/version ──► get_version_status()
        └─ Redis cache hit → no GitHub round-trip
        └─ miss → fetch_latest_release() (and re-cache), serialised by a
                  module-level asyncio.Lock so concurrent misses fan in.

    POST /api/admin/update ──► trigger_update()
        └─ asyncio.create_subprocess_exec(*UPDATE_COMMAND argv)
           First arg is enforced against UPDATE_COMMAND_ALLOWLIST so a
           compromised admin account can't pivot to arbitrary RCE.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Literal

import httpx

from _version import __version__
from cache.redis_cache import cache_get, cache_set, key_github_release
from config import settings

log = logging.getLogger(__name__)

# Cache lifetime is intentionally longer than UPDATE_CHECK_INTERVAL_HOURS so a
# missed scheduler tick (pod restart, clock skew) doesn't open a fetch storm.
CACHE_TTL_SECONDS = (settings.UPDATE_CHECK_INTERVAL_HOURS + 1) * 60 * 60
GITHUB_TIMEOUT_SECONDS = 10.0
UPDATE_TIMEOUT_SECONDS = 180.0

UpdateStatus = Literal["started", "not_configured", "failed"]
STATUS_STARTED: UpdateStatus = "started"
STATUS_NOT_CONFIGURED: UpdateStatus = "not_configured"
STATUS_FAILED: UpdateStatus = "failed"

_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")
_fetch_lock = asyncio.Lock()


def _parse_semver(tag: str) -> tuple[int, int, int] | None:
    m = _TAG_RE.match(tag.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _is_newer(latest: str, current: str) -> bool:
    a = _parse_semver(latest)
    b = _parse_semver(current)
    if a is None or b is None:
        return False
    return a > b


async def fetch_latest_release() -> dict[str, Any] | None:
    """Hit GitHub Releases API. Returns None on any failure."""
    if not settings.GITHUB_OWNER or not settings.GITHUB_REPO:
        return None
    url = (
        f"https://api.github.com/repos/{settings.GITHUB_OWNER}/"
        f"{settings.GITHUB_REPO}/releases/latest"
    )
    try:
        async with httpx.AsyncClient(timeout=GITHUB_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, headers={"Accept": "application/vnd.github+json"})
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.warning("github.release.fetch_failed", extra={"error": str(exc)})
        return None

    return {
        "tag": data.get("tag_name", ""),
        "name": data.get("name") or data.get("tag_name", ""),
        "html_url": data.get("html_url", ""),
        "published_at": data.get("published_at", ""),
    }


async def refresh_release_cache() -> None:
    """Scheduler entry point. Fetches and caches; never raises."""
    info = await fetch_latest_release()
    if info is None:
        return
    await cache_set(key_github_release(), json.dumps(info), CACHE_TTL_SECONDS)
    log.info("github.release.cached", extra={"tag": info.get("tag")})


async def force_refresh_status() -> dict[str, Any]:
    """Bypass cache, hit GitHub directly, then return current status.

    Used by `POST /api/admin/version/check` so an admin can trigger a fresh
    lookup without waiting for the next scheduler tick. Concurrent admins
    clicking simultaneously fan in via the same `_fetch_lock` so we still
    only emit one GitHub request per burst.
    """
    async with _fetch_lock:
        info = await fetch_latest_release()
        if info is not None:
            await cache_set(key_github_release(), json.dumps(info), CACHE_TTL_SECONDS)
        log.info("github.release.manual_check", extra={"tag": (info or {}).get("tag", "")})
    return await get_version_status()


async def _read_cached() -> dict[str, Any] | None:
    cached = await cache_get(key_github_release())
    if not cached:
        return None
    try:
        return json.loads(cached)
    except json.JSONDecodeError:
        log.warning("github.release.cache_corrupt", extra={"len": len(cached)})
        return None


async def get_version_status() -> dict[str, Any]:
    """Return current/latest version + update_available flag.

    Cache-first: if no cache and GitHub is unreachable, returns
    update_available=False with latest=current rather than failing the request.
    """
    info = await _read_cached()
    if info is None:
        # Fan-in concurrent misses behind a lock so we don't herd GitHub.
        async with _fetch_lock:
            info = await _read_cached()      # second check inside lock
            if info is None:
                info = await fetch_latest_release()
                if info is not None:
                    await cache_set(key_github_release(), json.dumps(info), CACHE_TTL_SECONDS)

    if info is None:
        return {
            "current": __version__,
            "latest": __version__,
            "update_available": False,
            "html_url": "",
            "published_at": "",
        }

    latest_tag = info.get("tag", "")
    return {
        "current": __version__,
        "latest": latest_tag.lstrip("v") or __version__,
        "update_available": _is_newer(latest_tag, __version__),
        "html_url": info.get("html_url", ""),
        "published_at": info.get("published_at", ""),
    }


async def trigger_update() -> dict[str, Any]:
    """Fire the configured update command.

    Returns a status dict — never raises. Stdout/stderr are logged but not
    returned to the client to avoid leaking deployment internals.
    """
    try:
        argv = settings.update_command_argv
    except ValueError as exc:
        log.error("admin.update.invalid_config", extra={"error": str(exc)})
        return {"status": STATUS_FAILED, "message": "UPDATE_COMMAND misconfigured"}

    if not argv:
        return {
            "status": STATUS_NOT_CONFIGURED,
            "message": "UPDATE_COMMAND is empty. Set it in .env to enable one-click updates.",
        }

    program = argv[0]
    allowlist = settings.update_command_allowlist
    program_name = program.rsplit("/", 1)[-1]
    if program not in allowlist and program_name not in allowlist:
        log.error("admin.update.blocked", extra={"program": program_name})
        return {"status": STATUS_FAILED, "message": "update command not in allowlist"}

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=UPDATE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()    # reap so we don't leak a zombie
            log.error("admin.update.timeout", extra={"timeout_s": UPDATE_TIMEOUT_SECONDS})
            return {"status": STATUS_FAILED, "message": "update command timed out"}

        if proc.returncode == 0:
            log.info("admin.update.started", extra={"stdout_bytes": len(stdout or b"")})
            return {"status": STATUS_STARTED, "message": "update command dispatched"}

        log.error(
            "admin.update.failed",
            extra={
                "returncode": proc.returncode,
                "stderr": (stderr or b"").decode(errors="replace")[:500],
            },
        )
        return {"status": STATUS_FAILED, "message": f"update command exit={proc.returncode}"}
    except Exception as exc:
        log.exception("admin.update.exception")
        return {"status": STATUS_FAILED, "message": str(exc)}
