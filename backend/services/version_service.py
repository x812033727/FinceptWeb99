"""GitHub release polling + admin-triggered update.

Flow:
    scheduler (every UPDATE_CHECK_INTERVAL_HOURS) ──► refresh_release_cache()
        └─ fetch_latest_release() ──► Redis (TTL 1h)

    GET /api/system/version ──► get_version_status()
        └─ Redis cache hit → no GitHub round-trip
        └─ miss → fetch_latest_release() (and re-cache)

    POST /api/admin/update ──► trigger_update()
        └─ asyncio.create_subprocess_shell(UPDATE_COMMAND)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from _version import __version__
from cache.redis_cache import cache_get, cache_set, key_github_release
from config import settings

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60 * 60          # 1 hour
GITHUB_TIMEOUT_SECONDS = 10.0
UPDATE_TIMEOUT_SECONDS = 60.0

_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


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


async def get_version_status() -> dict[str, Any]:
    """Return current/latest version + update_available flag.

    Cache-first: if no cache and GitHub is unreachable, returns
    update_available=False with latest=current rather than failing the request.
    """
    cached = await cache_get(key_github_release())
    info: dict[str, Any] | None
    if cached:
        try:
            info = json.loads(cached)
        except json.JSONDecodeError:
            info = None
    else:
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
    cmd = settings.UPDATE_COMMAND.strip()
    if not cmd:
        return {
            "status": "not_configured",
            "message": "UPDATE_COMMAND is empty. Set it in .env to enable one-click updates.",
        }

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=UPDATE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            log.error("admin.update.timeout", extra={"timeout_s": UPDATE_TIMEOUT_SECONDS})
            return {"status": "failed", "message": "update command timed out"}

        if proc.returncode == 0:
            log.info("admin.update.started", extra={"stdout_bytes": len(stdout or b"")})
            return {"status": "started", "message": "update command dispatched"}

        log.error(
            "admin.update.failed",
            extra={
                "returncode": proc.returncode,
                "stderr": (stderr or b"").decode(errors="replace")[:500],
            },
        )
        return {"status": "failed", "message": f"update command exit={proc.returncode}"}
    except Exception as exc:
        log.exception("admin.update.exception")
        return {"status": "failed", "message": str(exc)}
