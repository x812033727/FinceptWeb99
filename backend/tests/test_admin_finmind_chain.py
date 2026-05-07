"""Tests for `/api/admin/finmind/chain*` — chain control + monitor.

The HTTP surface is thin (4 endpoints that delegate to
`services.finmind_chain_service`). These tests pin the auth gate and
the 409 behavior; the service-level state machine is covered in
`test_finmind_chain_service.py`."""
from __future__ import annotations

import os

# Force in-memory SQLite for the FinMind subsystem BEFORE any of its
# modules are imported (matches `test_admin_finmind_proxy.py`).
os.environ.setdefault(
    "FINMIND_DATABASE_URL", "sqlite+aiosqlite:///:memory:"
)

import pytest  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from models.user import User, UserRole  # noqa: E402


async def _register_login(client: AsyncClient, email: str) -> str:
    await client.post(
        "/api/auth/register",
        json={"email": email, "password": "Test1234!"},
    )
    r = await client.post(
        "/api/auth/login",
        json={"email": email, "password": "Test1234!"},
    )
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _promote_to_admin(
    db: AsyncSession, email: str, client: AsyncClient
) -> str:
    from sqlalchemy import select

    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one()
    user.role = UserRole.admin
    await db.commit()
    r = await client.post(
        "/api/auth/login",
        json={"email": email, "password": "Test1234!"},
    )
    return r.json()["access_token"]


@pytest.fixture
def stub_chain(monkeypatch):
    """Stub the orchestration service so the API tests don't touch
    redis or asyncio.create_task. Returns a dict of mocks the test
    can configure."""
    from services import finmind_chain_service as chain

    state = {
        "status": "idle",
        "queue": [],
        "current_dataset": None,
        "current_symbol": None,
        "chunks_done": 0,
        "chunks_total": 0,
        "chunks_failed": 0,
        "started_at": None,
        "last_chunk_at": None,
        "stop_requested": False,
        "recent_errors": [],
        "quota_used": 0,
        "quota_limit": 6000,
        "host_chain_likely_active": False,
        "default_datasets": list(chain.DEFAULT_DATASETS),
    }
    calls = {"start": [], "stop": 0, "reset_stuck": 0}
    raise_on_start: list[Exception] = []

    async def _get_state():
        return dict(state)

    async def _start(datasets, days=3650, reset_stuck_first=True):
        if raise_on_start:
            raise raise_on_start[0]
        calls["start"].append({
            "datasets": datasets, "days": days,
            "reset_stuck_first": reset_stuck_first,
        })
        state["status"] = "running"
        state["queue"] = list(datasets)
        return dict(state)

    async def _stop():
        calls["stop"] += 1
        state["status"] = "stopping"
        state["stop_requested"] = True
        return dict(state)

    async def _reset_stuck():
        calls["reset_stuck"] += 1
        return 7

    monkeypatch.setattr(chain, "get_state", _get_state)
    monkeypatch.setattr(chain, "start_chain", _start)
    monkeypatch.setattr(chain, "stop_chain", _stop)
    monkeypatch.setattr(chain, "reset_stuck", _reset_stuck)

    return {"state": state, "calls": calls, "raise_on_start": raise_on_start}


@pytest.mark.asyncio
async def test_chain_endpoints_require_auth(client, stub_chain):
    for method, path in [
        ("get", "/api/admin/finmind/chain"),
        ("post", "/api/admin/finmind/chain/start"),
        ("post", "/api/admin/finmind/chain/stop"),
        ("post", "/api/admin/finmind/chain/reset-stuck"),
    ]:
        if method == "get":
            r = await client.get(path)
        else:
            r = await client.post(path, json={})
        assert r.status_code == 401, f"{method} {path} -> {r.status_code}"


@pytest.mark.asyncio
async def test_chain_endpoints_reject_non_admin(client, stub_chain):
    token = await _register_login(client, "viewer_chain@test.com")
    r = await client.get(
        "/api/admin/finmind/chain", headers=_auth(token),
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_get_chain_returns_idle_state(client, db_session, stub_chain):
    email = "admin_chain_get@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/chain", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "idle"
    assert "TaiwanStockPriceAdj" in body["default_datasets"]
    assert body["quota_limit"] == 6000


@pytest.mark.asyncio
async def test_start_chain_with_default_payload(
    client, db_session, stub_chain,
):
    email = "admin_chain_start@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/chain/start",
        json={},  # rely on Pydantic defaults
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert len(stub_chain["calls"]["start"]) == 1
    call = stub_chain["calls"]["start"][0]
    assert "TaiwanStockPriceAdj" in call["datasets"]
    assert call["days"] == 3650


@pytest.mark.asyncio
async def test_start_chain_with_custom_subset(
    client, db_session, stub_chain,
):
    email = "admin_chain_subset@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/chain/start",
        json={
            "datasets": ["TaiwanStockMonthRevenue", "TaiwanStockDividend"],
            "days": 365,
        },
        headers=_auth(token),
    )
    assert r.status_code == 200
    call = stub_chain["calls"]["start"][0]
    assert call["datasets"] == [
        "TaiwanStockMonthRevenue", "TaiwanStockDividend",
    ]
    assert call["days"] == 365


@pytest.mark.asyncio
async def test_start_chain_returns_409_when_already_running(
    client, db_session, stub_chain,
):
    from services.finmind_chain_service import ChainAlreadyRunning

    stub_chain["raise_on_start"].append(
        ChainAlreadyRunning("chain already running on another worker")
    )

    email = "admin_chain_409@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/chain/start",
        json={},
        headers=_auth(token),
    )
    assert r.status_code == 409
    assert "already running" in r.json()["detail"]


@pytest.mark.asyncio
async def test_start_chain_returns_409_when_host_chain_active(
    client, db_session, stub_chain,
):
    from services.finmind_chain_service import ChainConflict

    stub_chain["raise_on_start"].append(
        ChainConflict("host-level finmind_chain.sh appears to be running")
    )

    email = "admin_chain_host409@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/chain/start",
        json={},
        headers=_auth(token),
    )
    assert r.status_code == 409
    assert "finmind_chain.sh" in r.json()["detail"]


@pytest.mark.asyncio
async def test_stop_chain(client, db_session, stub_chain):
    email = "admin_chain_stop@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/chain/stop", headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "stopping"
    assert stub_chain["calls"]["stop"] == 1


@pytest.mark.asyncio
async def test_reset_stuck(client, db_session, stub_chain):
    email = "admin_chain_reset@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/chain/reset-stuck", headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json() == {"reset": 7}
    assert stub_chain["calls"]["reset_stuck"] == 1
