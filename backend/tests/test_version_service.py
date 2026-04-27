"""Pure unit tests for version_service — no DB, no HTTP, no Redis.

Tests the semver comparator and the cache + GitHub fetch + trigger_update
logic with mocks. Runs locally without external services.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from _version import __version__
from services import version_service as vs


# ── semver comparator ─────────────────────────────────────────────

def test_is_newer_strict_greater():
    assert vs._is_newer("v0.2.0", "0.1.0") is True
    assert vs._is_newer("0.2.0", "0.1.0") is True
    assert vs._is_newer("v1.0.0", "0.9.99") is True


def test_is_newer_equal_returns_false():
    assert vs._is_newer("0.1.0", "0.1.0") is False
    assert vs._is_newer("v0.1.0", "0.1.0") is False


def test_is_newer_lower_returns_false():
    assert vs._is_newer("v0.1.0", "0.2.0") is False
    assert vs._is_newer("0.0.9", "0.1.0") is False


def test_is_newer_garbage_input_returns_false():
    assert vs._is_newer("not-a-version", "0.1.0") is False
    assert vs._is_newer("0.1.0", "garbage") is False


# ── fetch_latest_release ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_latest_release_success(monkeypatch):
    payload = {
        "tag_name": "v0.2.0",
        "name": "v0.2.0",
        "html_url": "https://github.com/owner/repo/releases/tag/v0.2.0",
        "published_at": "2026-04-25T00:00:00Z",
    }

    class _Resp:
        status_code = 200
        def json(self):
            return payload
        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(vs.httpx, "AsyncClient", _Client)
    info = await vs.fetch_latest_release()
    assert info == {
        "tag": "v0.2.0",
        "name": "v0.2.0",
        "html_url": "https://github.com/owner/repo/releases/tag/v0.2.0",
        "published_at": "2026-04-25T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_fetch_latest_release_404(monkeypatch):
    class _Resp:
        status_code = 404
        def json(self):
            return {}
        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, *a, **k): return _Resp()

    monkeypatch.setattr(vs.httpx, "AsyncClient", _Client)
    assert await vs.fetch_latest_release() is None


@pytest.mark.asyncio
async def test_fetch_latest_release_network_error(monkeypatch):
    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, *a, **k):
            raise OSError("network unreachable")

    monkeypatch.setattr(vs.httpx, "AsyncClient", _Client)
    assert await vs.fetch_latest_release() is None


# ── get_version_status ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_version_status_cache_hit_returns_update_available():
    cached = json.dumps({
        "tag": "v9.9.9",
        "name": "v9.9.9",
        "html_url": "https://example.com/r",
        "published_at": "2026-04-25T00:00:00Z",
    })
    with patch.object(vs, "cache_get", AsyncMock(return_value=cached)):
        status = await vs.get_version_status()

    assert status["current"] == __version__
    assert status["latest"] == "9.9.9"
    assert status["update_available"] is True
    assert status["html_url"] == "https://example.com/r"


@pytest.mark.asyncio
async def test_version_status_cache_miss_falls_back_to_fetch():
    info = {
        "tag": "v0.1.0",
        "name": "v0.1.0",
        "html_url": "",
        "published_at": "",
    }
    with patch.object(vs, "cache_get", AsyncMock(return_value=None)), \
         patch.object(vs, "cache_set", AsyncMock()) as set_mock, \
         patch.object(vs, "fetch_latest_version", AsyncMock(return_value=info)):
        status = await vs.get_version_status()

    set_mock.assert_awaited_once()
    assert status["latest"] == "0.1.0"
    assert status["update_available"] is False


@pytest.mark.asyncio
async def test_version_status_unreachable_github_returns_safe_default():
    with patch.object(vs, "cache_get", AsyncMock(return_value=None)), \
         patch.object(vs, "fetch_latest_version", AsyncMock(return_value=None)):
        status = await vs.get_version_status()

    assert status["current"] == __version__
    assert status["latest"] == __version__
    assert status["update_available"] is False


# ── force_refresh_status ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_force_refresh_status_writes_cache_and_returns_fresh():
    # Pick a tag that's guaranteed newer than __version__ regardless of how
    # the current release moves over time. (A previous incarnation of this
    # test hard-coded 'v0.2.0' and broke the day we shipped 0.2.0.)
    fresh = {
        "tag": "v99.0.0",
        "name": "v99.0.0",
        "html_url": "https://example.com/r",
        "published_at": "2026-04-25T00:00:00Z",
    }
    with patch.object(vs, "fetch_latest_version", AsyncMock(return_value=fresh)) as fetch_mock, \
         patch.object(vs, "cache_set", AsyncMock()) as set_mock, \
         patch.object(vs, "cache_get", AsyncMock(return_value=json.dumps(fresh))):
        status = await vs.force_refresh_status()

    fetch_mock.assert_awaited_once()
    set_mock.assert_awaited_once()
    assert status["latest"] == "99.0.0"
    assert status["update_available"] is True


@pytest.mark.asyncio
async def test_force_refresh_status_handles_github_failure():
    """If GitHub is unreachable during a manual check, we don't blow up the
    cache and we fall through to whatever get_version_status returns."""
    with patch.object(vs, "fetch_latest_version", AsyncMock(return_value=None)), \
         patch.object(vs, "cache_set", AsyncMock()) as set_mock, \
         patch.object(vs, "cache_get", AsyncMock(return_value=None)):
        status = await vs.force_refresh_status()

    set_mock.assert_not_awaited()
    assert status["update_available"] is False


# ── fetch_default_branch_version (fallback path) ──────────────────

class _RawResp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


def _client_returning(resp):
    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, *a, **k): return resp
    return _Client


@pytest.mark.asyncio
async def test_fetch_default_branch_version_parses_version(monkeypatch):
    monkeypatch.setattr(
        vs.httpx, "AsyncClient",
        _client_returning(_RawResp(200, '__version__ = "0.3.7"\n')),
    )
    info = await vs.fetch_default_branch_version()
    assert info is not None
    assert info["tag"] == "v0.3.7"
    assert info["name"] == "v0.3.7"
    assert info["published_at"] == ""
    assert "backend/_version.py" in info["html_url"]


@pytest.mark.asyncio
async def test_fetch_default_branch_version_404_returns_none(monkeypatch):
    monkeypatch.setattr(
        vs.httpx, "AsyncClient", _client_returning(_RawResp(404)),
    )
    assert await vs.fetch_default_branch_version() is None


@pytest.mark.asyncio
async def test_fetch_default_branch_version_unparseable_returns_none(monkeypatch):
    monkeypatch.setattr(
        vs.httpx, "AsyncClient",
        _client_returning(_RawResp(200, "# no version here\n")),
    )
    assert await vs.fetch_default_branch_version() is None


@pytest.mark.asyncio
async def test_fetch_latest_version_prefers_release():
    release_info = {"tag": "v9.9.9", "name": "v9.9.9", "html_url": "", "published_at": ""}
    with patch.object(vs, "fetch_latest_release", AsyncMock(return_value=release_info)), \
         patch.object(vs, "fetch_default_branch_version", AsyncMock()) as fb_mock:
        info = await vs.fetch_latest_version()

    fb_mock.assert_not_awaited()
    assert info == release_info


@pytest.mark.asyncio
async def test_fetch_latest_version_falls_back_when_no_release():
    fb_info = {"tag": "v0.1.1", "name": "v0.1.1", "html_url": "", "published_at": ""}
    with patch.object(vs, "fetch_latest_release", AsyncMock(return_value=None)), \
         patch.object(vs, "fetch_default_branch_version", AsyncMock(return_value=fb_info)):
        info = await vs.fetch_latest_version()

    assert info == fb_info


# ── trigger_update ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trigger_update_not_configured(monkeypatch):
    monkeypatch.setattr(vs.settings, "UPDATE_COMMAND", "")
    result = await vs.trigger_update()
    assert result["status"] == "not_configured"
    # Operators with an empty UPDATE_COMMAND get actionable guidance
    # in the message — Docker Compose / kubectl / helm examples.
    assert "docker" in result["message"].lower()
    assert "kubectl" in result["message"].lower()


@pytest.mark.asyncio
async def test_trigger_update_success(monkeypatch):
    # New format: JSON-encoded argv list. argv[0] basename must be in the
    # allowlist so the call survives the safety check.
    monkeypatch.setattr(vs.settings, "UPDATE_COMMAND", '["true"]')
    monkeypatch.setattr(vs.settings, "UPDATE_COMMAND_ALLOWLIST", "true")

    class _Proc:
        returncode = 0
        async def communicate(self):
            return (b"", b"")
        def kill(self):
            pass

    async def _exec(*a, **k):
        return _Proc()

    monkeypatch.setattr(vs.asyncio, "create_subprocess_exec", _exec)
    result = await vs.trigger_update()
    assert result["status"] == "started"


@pytest.mark.asyncio
async def test_trigger_update_nonzero_exit(monkeypatch):
    monkeypatch.setattr(vs.settings, "UPDATE_COMMAND", '["false"]')
    monkeypatch.setattr(vs.settings, "UPDATE_COMMAND_ALLOWLIST", "false")

    class _Proc:
        returncode = 1
        async def communicate(self):
            return (b"", b"command failed")
        def kill(self):
            pass

    async def _exec(*a, **k):
        return _Proc()

    monkeypatch.setattr(vs.asyncio, "create_subprocess_exec", _exec)
    result = await vs.trigger_update()
    assert result["status"] == "failed"
    assert "exit=1" in result["message"]


@pytest.mark.asyncio
async def test_trigger_update_blocked_by_allowlist(monkeypatch):
    monkeypatch.setattr(vs.settings, "UPDATE_COMMAND", '["rm","-rf","/"]')
    monkeypatch.setattr(vs.settings, "UPDATE_COMMAND_ALLOWLIST", "docker,kubectl")
    result = await vs.trigger_update()
    assert result["status"] == "failed"
    assert "allowlist" in result["message"]


@pytest.mark.asyncio
async def test_trigger_update_invalid_json(monkeypatch):
    # Plain string (legacy shell style) is no longer accepted.
    monkeypatch.setattr(vs.settings, "UPDATE_COMMAND", "docker compose pull")
    result = await vs.trigger_update()
    assert result["status"] == "failed"
    assert "misconfigured" in result["message"]
