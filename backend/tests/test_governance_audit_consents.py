import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.governance import UserConsent


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register_login(client: AsyncClient, email: str) -> str:
    await client.post("/api/auth/register", json={"email": email, "password": "Test1234!"})
    response = await client.post("/api/auth/login", json={"email": email, "password": "Test1234!"})
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_mutating_requests_are_audited_without_secret_payloads(client: AsyncClient):
    token = await _register_login(client, "audit@example.com")
    created = await client.post(
        "/api/auth/api-keys", json={"name": "audit-key"}, headers=_auth(token),
    )
    assert created.status_code == 201
    assert created.headers.get("X-Audit-Recorded") == "1"


@pytest.mark.asyncio
async def test_failed_login_is_audited(client: AsyncClient):
    response = await client.post("/api/auth/login", json={
        "email": "nobody@example.com", "password": "WrongPass99!",
    })
    assert response.status_code == 401
    assert response.headers.get("X-Audit-Recorded") == "1"


@pytest.mark.asyncio
async def test_current_consent_versions_can_be_accepted_once(
    client: AsyncClient, db_session: AsyncSession,
):
    token = await _register_login(client, "consent@example.com")
    headers = _auth(token)

    initial = await client.get("/api/auth/consents", headers=headers)
    assert initial.status_code == 200
    assert {row["document"] for row in initial.json()} == {
        "terms", "privacy", "ai_data_disclosure",
    }
    assert all(not row["accepted"] for row in initial.json())

    accepted = await client.post("/api/auth/consents", headers=headers, json={
        "document": "ai_data_disclosure",
        "version": settings.AI_DATA_DISCLOSURE_VERSION,
    })
    assert accepted.status_code == 201
    assert accepted.json()["accepted"] is True

    duplicate = await client.post("/api/auth/consents", headers=headers, json={
        "document": "ai_data_disclosure",
        "version": settings.AI_DATA_DISCLOSURE_VERSION,
    })
    assert duplicate.status_code == 201
    rows = list((await db_session.scalars(select(UserConsent).where(
        UserConsent.document == "ai_data_disclosure",
    ))).all())
    assert len(rows) == 1

    stale = await client.post("/api/auth/consents", headers=headers, json={
        "document": "privacy", "version": "old-version",
    })
    assert stale.status_code == 400

    current = await client.get("/api/auth/consents", headers=headers)
    disclosure = next(row for row in current.json() if row["document"] == "ai_data_disclosure")
    assert disclosure["accepted"] is True
    assert disclosure["accepted_at"] is not None
