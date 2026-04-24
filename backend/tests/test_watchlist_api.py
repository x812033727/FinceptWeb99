"""Integration tests for watchlist CRUD endpoints."""
import pytest
from httpx import AsyncClient


async def _auth_headers(client: AsyncClient) -> dict[str, str]:
    await client.post("/api/auth/register", json={
        "email": "wl_test@example.com",
        "password": "ValidPass99!",
    })
    r = await client.post("/api/auth/login", json={
        "email": "wl_test@example.com",
        "password": "ValidPass99!",
    })
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_list_watchlists_empty(client: AsyncClient):
    h = await _auth_headers(client)
    r = await client.get("/api/watchlist", headers=h)
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_create_watchlist(client: AsyncClient):
    h = await _auth_headers(client)
    r = await client.post("/api/watchlist", json={"name": "My List"}, headers=h)
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "My List"
    assert "id" in data


@pytest.mark.asyncio
async def test_add_and_list_item(client: AsyncClient):
    h = await _auth_headers(client)

    wl = (await client.post("/api/watchlist", json={"name": "Tech"}, headers=h)).json()
    wid = wl["id"]

    r = await client.post(f"/api/watchlist/{wid}/items",
                          json={"symbol": "AAPL", "market": "US"}, headers=h)
    assert r.status_code == 201
    item = r.json()
    assert item["symbol"] == "AAPL"

    # List should now contain the watchlist with the item
    lists = (await client.get("/api/watchlist", headers=h)).json()
    assert any(w["id"] == wid for w in lists)
    target = next(w for w in lists if w["id"] == wid)
    assert any(i["symbol"] == "AAPL" for i in target["items"])


@pytest.mark.asyncio
async def test_remove_item(client: AsyncClient):
    h = await _auth_headers(client)

    wl = (await client.post("/api/watchlist", json={"name": "To Remove"}, headers=h)).json()
    wid = wl["id"]
    item = (await client.post(f"/api/watchlist/{wid}/items",
                               json={"symbol": "TSLA", "market": "US"}, headers=h)).json()
    iid = item["id"]

    r = await client.delete(f"/api/watchlist/{wid}/items/{iid}", headers=h)
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_delete_watchlist(client: AsyncClient):
    h = await _auth_headers(client)

    wl = (await client.post("/api/watchlist", json={"name": "Delete Me"}, headers=h)).json()
    wid = wl["id"]

    r = await client.delete(f"/api/watchlist/{wid}", headers=h)
    assert r.status_code == 204

    lists = (await client.get("/api/watchlist", headers=h)).json()
    assert all(w["id"] != wid for w in lists)


@pytest.mark.asyncio
async def test_watchlist_requires_auth(client: AsyncClient):
    r = await client.get("/api/watchlist")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_duplicate_item_rejected(client: AsyncClient):
    h = await _auth_headers(client)
    wl = (await client.post("/api/watchlist", json={"name": "Dupes"}, headers=h)).json()
    wid = wl["id"]
    await client.post(f"/api/watchlist/{wid}/items",
                      json={"symbol": "MSFT", "market": "US"}, headers=h)
    r = await client.post(f"/api/watchlist/{wid}/items",
                          json={"symbol": "MSFT", "market": "US"}, headers=h)
    assert r.status_code in (400, 409)
