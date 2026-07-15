import pytest
from httpx import AsyncClient


async def _token(client: AsyncClient, email: str) -> str:
    await client.post("/api/auth/register", json={"email": email, "password": "Pass99!!"})
    response = await client.post("/api/auth/login", json={"email": email, "password": "Pass99!!"})
    return response.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_thesis_crud_review_and_timeline(client: AsyncClient):
    token = await _token(client, "thesis-owner@test.com")
    created = await client.post(
        "/api/theses",
        headers=_headers(token),
        json={
            "market": "TW",
            "symbol": "2330",
            "title": "AI demand compounds",
            "core_case": "Advanced-node demand remains durable.",
            "catalysts": ["2nm ramp"],
            "risks": ["geopolitics"],
            "valuation": {"low": 800, "high": 1100, "currency": "TWD"},
            "watch_conditions": ["monthly revenue YoY below 10%"],
            "review_date": "2026-08-01",
        },
    )
    assert created.status_code == 201
    thesis_id = created.json()["id"]

    updated = await client.patch(
        f"/api/theses/{thesis_id}", headers=_headers(token), json={"status": "watching"},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "watching"

    review = await client.post(
        f"/api/theses/{thesis_id}/review",
        headers=_headers(token),
        json={"conclusion": "weakened", "notes": "Margin guidance softened.", "next_review_date": "2026-09-01"},
    )
    assert review.status_code == 201
    assert review.json()["event_type"] == "review"

    timeline = await client.get(f"/api/theses/{thesis_id}/timeline", headers=_headers(token))
    assert timeline.status_code == 200
    assert len(timeline.json()) == 1
    assert timeline.json()[0]["details"]["conclusion"] == "weakened"

    listing = await client.get("/api/theses", headers=_headers(token))
    assert [row["id"] for row in listing.json()] == [thesis_id]


@pytest.mark.asyncio
async def test_thesis_cross_user_resources_are_indistinguishable_from_missing(client: AsyncClient):
    owner = await _token(client, "thesis-owner-404@test.com")
    stranger = await _token(client, "thesis-stranger-404@test.com")
    created = await client.post(
        "/api/theses", headers=_headers(owner),
        json={"market": "US", "symbol": "AAPL", "title": "Services mix", "core_case": "Recurring revenue grows."},
    )
    thesis_id = created.json()["id"]

    for method, path, payload in (
        (client.get, f"/api/theses/{thesis_id}", None),
        (client.patch, f"/api/theses/{thesis_id}", {"status": "closed"}),
        (client.delete, f"/api/theses/{thesis_id}", None),
        (client.get, f"/api/theses/{thesis_id}/timeline", None),
        (client.post, f"/api/theses/{thesis_id}/review", {"conclusion": "unchanged", "notes": "probe"}),
    ):
        response = await method(path, headers=_headers(stranger), **({"json": payload} if payload is not None else {}))
        assert response.status_code == 404

    owner_view = await client.get(f"/api/theses/{thesis_id}", headers=_headers(owner))
    assert owner_view.status_code == 200


@pytest.mark.asyncio
async def test_structured_watch_conditions_are_validated_and_persisted(client: AsyncClient):
    token = await _token(client, "thesis-conditions@test.com")
    condition = {
        "label": "Revenue growth floor",
        "metric": "revenue_yoy_pct",
        "operator": "lt",
        "threshold": 10,
    }
    created = await client.post(
        "/api/theses", headers=_headers(token),
        json={
            "market": "TW", "symbol": "2330", "title": "Growth",
            "core_case": "Growth persists.", "watch_conditions": [condition],
        },
    )
    assert created.status_code == 201
    stored = created.json()["watch_conditions"][0]
    assert {key: stored[key] for key in condition} == condition
    assert len(stored["id"]) == 32

    unsupported = await client.post(
        "/api/theses", headers=_headers(token),
        json={
            "market": "US", "symbol": "AAPL", "title": "US growth",
            "core_case": "Services grow.", "watch_conditions": [condition],
        },
    )
    assert unsupported.status_code == 422

    invalid_metric = await client.post(
        "/api/theses", headers=_headers(token),
        json={
            "market": "TW", "symbol": "2330", "title": "Invalid",
            "core_case": "Invalid condition.",
            "watch_conditions": [{**condition, "metric": "unsupported"}],
        },
    )
    assert invalid_metric.status_code == 422
