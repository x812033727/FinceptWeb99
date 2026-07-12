"""Tests for `/api/admin/finmind/*` — main-app admin proxy to the
FinMind clone.

The proxy crosses the architectural boundary documented in CLAUDE.md
(FinMind clone is normally consumed via HTTP, not in-process imports)
ONLY for the AdminPage so the React app uses its existing JWT admin
auth. This test file pins the contract.

Setup gymnastics: the FinMind subsystem owns its own DB engine. To
make the proxy work in the test env (which uses the main app's
in-memory SQLite), we override `get_finmind_db` to yield a dedicated
SQLite session built from `finmind.db.base.Base.metadata` only.
"""
from __future__ import annotations

import os

# Force in-memory SQLite for the FinMind subsystem BEFORE any of its
# modules are imported (matches `backend/finmind/tests/conftest.py`).
os.environ.setdefault(
    "FINMIND_DATABASE_URL", "sqlite+aiosqlite:///:memory:"
)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession, async_sessionmaker, create_async_engine,
)

from models.user import User, UserRole  # noqa: E402


# ── Helpers (mirror test_admin_api.py) ──────────────────────────


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


# ── FinMind DB fixture ──────────────────────────────────────────


@pytest_asyncio.fixture
async def finmind_db_override():
    """Build an in-memory FinMind DB + override the proxy's
    `get_finmind_db` dep so it yields against this engine instead of
    the configured `FINMIND_DATABASE_URL` (which would try to talk to
    a real Postgres in CI)."""
    # Late imports — keep these inside the fixture so the test module
    # itself doesn't pull finmind.db.session at collection time
    # (which eagerly creates an asyncpg engine if the env var isn't
    # patched in time).
    import finmind.models  # noqa: F401  (registers all FinMind models)
    from finmind.db.base import Base as FinmindBase

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(FinmindBase.metadata.create_all)

    SessionLocal = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False,
    )

    async def _override():
        async with SessionLocal() as s:
            yield s

    from finmind.db.session import get_finmind_db
    from main import app

    app.dependency_overrides[get_finmind_db] = _override
    try:
        yield SessionLocal
    finally:
        app.dependency_overrides.pop(get_finmind_db, None)
        await engine.dispose()


async def _seed_catalog(SessionLocal) -> None:
    """Wrap finmind.scripts.init_db.seed_dataset_sources in a fresh
    session bound to our test engine (the script's own session
    factory is monkeypatched per-test in the finmind subsystem's
    conftest, but here we're in main backend test land)."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from data.tw.finmind_datasets import find_dataset
    from finmind.dataset_catalog import all_entries
    from finmind.models.dataset_source import DatasetSource

    rows = []
    for category, entry in all_entries():
        spec = find_dataset(entry.dataset_code)
        rows.append({
            "dataset_code": entry.dataset_code,
            "category": category,
            "description_zh": spec.description,
            "local_table": entry.local_table,
            "per_symbol": spec.per_symbol,
            "primary_source": entry.primary_source,
            "fallback_source": entry.fallback_source,
            "active_source": entry.primary_source,
            "sponsor_tier": spec.sponsor_tier,
            "ingest_freq": entry.ingest_freq,
        })

    async with SessionLocal() as s:
        stmt = sqlite_insert(DatasetSource).values(rows)
        await s.execute(stmt)
        await s.commit()


# ── Access control ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_proxy_requires_auth(client, finmind_db_override):
    r = await client.get("/api/admin/finmind/datasets")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_proxy_rejects_non_admin(client, finmind_db_override):
    token = await _register_login(client, "viewer_finmind@test.com")
    r = await client.get(
        "/api/admin/finmind/datasets", headers=_auth(token),
    )
    assert r.status_code == 403


# ── Catalog list + PATCH ────────────────────────────────────────


# ── Setup checklist (first-run wizard) ──────────────────────────


@pytest.mark.asyncio
async def test_setup_status_fresh_db_lists_unmet_steps(
    client, db_session, finmind_db_override,
):
    """Fresh DB after just `create_all` (no init_db seed yet) →
    db_reachable=True, catalog_seeded=False, etc. The first failing
    step's fix_hint is surfaced as `next_action` so the operator
    knows exactly what to do."""
    email = "admin_fm_setup_fresh@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/setup-status", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()

    keys = [c["key"] for c in body["checks"]]
    assert keys == [
        "db_reachable",
        "catalog_seeded",
        "finmind_token_works",
        "universe_populated",
    ]

    by_key = {c["key"]: c for c in body["checks"]}
    # DB came up via the test fixture so step 1 passes.
    assert by_key["db_reachable"]["passed"] is True
    # No init_db seed → step 2 fails with a useful hint.
    assert by_key["catalog_seeded"]["passed"] is False
    assert "init_db" in by_key["catalog_seeded"]["fix_hint"]

    # Steps 3+ skipped while step 2 unmet.
    assert by_key["finmind_token_works"]["passed"] is False
    assert "skipped" in by_key["finmind_token_works"]["detail"]

    # next_action surfaces step 2's hint (first failure).
    assert body["next_action"] is not None
    assert "init_db" in body["next_action"]


@pytest.mark.asyncio
async def test_setup_status_after_seed_progresses_to_token_check(
    client, db_session, finmind_db_override, monkeypatch,
):
    """After init_db seed, catalog passes → next failure is the
    FinMind token probe. Verify the checklist walks forward."""
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    # Mock FinMind so we don't actually hit the network — token
    # works, returns rows.
    async def fake_query(dataset, data_id, start, end=None):
        return [{"stock_id": "2330"}]

    async def fake_get_token():
        return "valid-token"

    monkeypatch.setattr(
        "data.tw.finmind_connector._query", fake_query,
    )
    monkeypatch.setattr(
        "data.tw.finmind_connector._get_token", fake_get_token,
    )

    email = "admin_fm_setup_seeded@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/setup-status", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    by_key = {c["key"]: c for c in body["checks"]}
    assert by_key["catalog_seeded"]["passed"] is True
    assert by_key["finmind_token_works"]["passed"] is True
    # Universe still empty (TaiwanStockInfo never ran in this test) →
    # step 4 fails with the right hint.
    assert by_key["universe_populated"]["passed"] is False
    assert "TaiwanStockInfo" in by_key["universe_populated"]["fix_hint"]
    assert body["next_action"] is not None
    assert "TaiwanStockInfo" in body["next_action"]


@pytest.mark.asyncio
async def test_setup_status_all_pass_returns_null_next_action(
    client, db_session, finmind_db_override, monkeypatch,
):
    """All 4 checks green → next_action is None (UI graduates to
    'Setup complete')."""
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    async def fake_query(dataset, data_id, start, end=None):
        return [{"stock_id": "2330"}]

    async def fake_get_token():
        return "valid-token"

    monkeypatch.setattr(
        "data.tw.finmind_connector._query", fake_query,
    )
    monkeypatch.setattr(
        "data.tw.finmind_connector._get_token", fake_get_token,
    )

    # Insert at least one tw_stock_info row to satisfy step 4.
    from finmind.models.master import TwStockInfo
    async with SessionLocal() as s:
        s.add(TwStockInfo(
            market="TWSE", symbol="2330",
            name_zh="TSMC", source="finmind",
        ))
        await s.commit()

    email = "admin_fm_setup_done@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/setup-status", headers=_auth(token),
    )
    body = r.json()
    assert all(c["passed"] for c in body["checks"])
    assert body["next_action"] is None


@pytest.mark.asyncio
async def test_setup_status_token_failure_surfaces_diagnostic(
    client, db_session, finmind_db_override, monkeypatch,
):
    """Empty rows + no token → diagnostic distinguishes from the
    'token present but quota exhausted' case."""
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    async def fake_query(dataset, data_id, start, end=None):
        return []

    async def fake_get_token():
        return ""

    monkeypatch.setattr(
        "data.tw.finmind_connector._query", fake_query,
    )
    monkeypatch.setattr(
        "data.tw.finmind_connector._get_token", fake_get_token,
    )

    email = "admin_fm_setup_notoken@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/setup-status", headers=_auth(token),
    )
    body = r.json()
    by_key = {c["key"]: c for c in body["checks"]}
    assert by_key["finmind_token_works"]["passed"] is False
    assert "FINMIND_TOKEN empty" in by_key["finmind_token_works"]["detail"]


# ── Quick Start ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_quick_start_enables_curated_set(
    client, db_session, finmind_db_override,
):
    """Headline behavior: POST /quick-start flips enabled=true on the
    11 curated datasets. Verify both the response shape AND the
    underlying DB rows."""
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_qs_curated@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/quick-start", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    # Curated set has 11 datasets — but some may have empty
    # local_table (Phase 1 incomplete for them) so enabled_count <=11.
    assert body["enabled_count"] >= 5
    assert "TaiwanStockInfo" in body["enabled"]
    assert "TaiwanStockPrice" in body["enabled"]

    # Verify side effects in DB.
    from finmind.models.dataset_source import DatasetSource as DS
    async with SessionLocal() as s:
        info = await s.get(DS, "TaiwanStockInfo")
        price = await s.get(DS, "TaiwanStockPrice")
        assert info.enabled is True
        assert price.enabled is True


@pytest.mark.asyncio
async def test_quick_start_skips_datasets_without_local_table(
    client, db_session, finmind_db_override,
):
    """Datasets whose Phase 1 migration hasn't built a local_table
    end up in `skipped` with a clear reason rather than 4xx-ing the
    whole quick-start. The other recommended ones still flip on."""
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    # Force one of the curated datasets into "no local_table" state
    # to simulate Phase 1 still in progress for it.
    from finmind.models.dataset_source import DatasetSource as DS
    async with SessionLocal() as s:
        row = await s.get(DS, "TaiwanStockMonthRevenue")
        row.local_table = ""
        await s.commit()

    email = "admin_fm_qs_skip@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/quick-start", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    skipped_text = " ".join(body["skipped"])
    assert "TaiwanStockMonthRevenue" in skipped_text
    assert "no local_table" in skipped_text
    # Other datasets still got enabled.
    assert body["enabled_count"] >= 5


@pytest.mark.asyncio
async def test_quick_start_idempotent_on_second_call(
    client, db_session, finmind_db_override,
):
    """Second call is a no-op (every recommended already enabled).
    Response shows them in `enabled` but the count stays the same —
    the operator can repeat the action without double-effect."""
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_qs_idem@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r1 = await client.post(
        "/api/admin/finmind/quick-start", headers=_auth(token),
    )
    r2 = await client.post(
        "/api/admin/finmind/quick-start", headers=_auth(token),
    )
    assert r1.status_code == r2.status_code == 200
    # Same enabled count both times (idempotent invariant).
    assert r1.json()["enabled_count"] == r2.json()["enabled_count"]


@pytest.mark.asyncio
async def test_quick_start_requires_admin(
    client, db_session, finmind_db_override,
):
    token = await _register_login(client, "viewer_qs@test.com")
    r = await client.post(
        "/api/admin/finmind/quick-start", headers=_auth(token),
    )
    assert r.status_code == 403


# ── FinMind connection self-test ────────────────────────────────


@pytest.mark.asyncio
async def test_test_connection_ok_path(
    client, db_session, finmind_db_override, monkeypatch,
):
    """Happy path: connector returns rows → test reports ok=True
    with helpful message + counts."""
    async def fake_query(dataset, data_id, start, end=None):
        return [{"stock_id": "2330", "stock_name": "TSMC"}]

    async def fake_get_token():
        return "fake-token-for-test"

    monkeypatch.setattr("data.tw.finmind_connector._query", fake_query)
    monkeypatch.setattr(
        "data.tw.finmind_connector._get_token", fake_get_token,
    )

    email = "admin_fm_test_ok@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/test-connection", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["token_present"] is True
    assert body["rows_returned"] == 1
    assert body["dataset_tested"] == "TaiwanStockInfo"
    assert "connection OK" in body["message"]


@pytest.mark.asyncio
async def test_test_connection_warns_when_token_missing(
    client, db_session, finmind_db_override, monkeypatch,
):
    """Empty rows + no token → suggest setting FINMIND_TOKEN. The
    message is the operator's first hint at what's wrong."""
    async def fake_query(dataset, data_id, start, end=None):
        return []

    async def fake_get_token():
        return ""

    monkeypatch.setattr("data.tw.finmind_connector._query", fake_query)
    monkeypatch.setattr(
        "data.tw.finmind_connector._get_token", fake_get_token,
    )

    email = "admin_fm_test_notoken@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/test-connection", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["token_present"] is False
    assert "FINMIND_TOKEN is empty" in body["message"]


@pytest.mark.asyncio
async def test_test_connection_warns_when_quota_exhausted_with_token(
    client, db_session, finmind_db_override, monkeypatch,
):
    """Empty rows + token present → quota exhausted or tier issue.
    Different message from the no-token case so the operator can
    diagnose without server logs."""
    async def fake_query(dataset, data_id, start, end=None):
        return []

    async def fake_get_token():
        return "valid-but-rate-limited-token"

    monkeypatch.setattr("data.tw.finmind_connector._query", fake_query)
    monkeypatch.setattr(
        "data.tw.finmind_connector._get_token", fake_get_token,
    )

    email = "admin_fm_test_quota@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/test-connection", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["token_present"] is True
    assert "quota exhausted" in body["message"]


@pytest.mark.asyncio
async def test_test_connection_captures_connector_exception(
    client, db_session, finmind_db_override, monkeypatch,
):
    """Network unreachable etc. → exception caught + exposed in message
    rather than crashing to a 500. UI shows it as the diagnostic
    banner instead of "Request failed with status code 500"."""
    async def fake_query(dataset, data_id, start, end=None):
        raise ConnectionError("FinMind unreachable")

    async def fake_get_token():
        return "x"

    monkeypatch.setattr("data.tw.finmind_connector._query", fake_query)
    monkeypatch.setattr(
        "data.tw.finmind_connector._get_token", fake_get_token,
    )

    email = "admin_fm_test_err@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/test-connection", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "ConnectionError" in body["message"]
    assert "FinMind unreachable" in body["message"]


@pytest.mark.asyncio
async def test_test_connection_requires_admin(
    client, db_session, finmind_db_override,
):
    """Non-admin → 403. Gate same as every other admin proxy endpoint."""
    token = await _register_login(client, "viewer_fm_test@test.com")
    r = await client.post(
        "/api/admin/finmind/test-connection", headers=_auth(token),
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_proxy_lists_catalog_for_admin(
    client, db_session, finmind_db_override,
):
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_list@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/datasets", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    # 90 = 80 (FinMind TaiwanMarket DataList + 1 BuyBack legacy) + 5
    # macro datasets (TaiwanExchangeRate / InterestRate /
    # GovernmentBondsYield / CnnFearGreedIndex / CrudeOilPrices) + 5
    # crypto datasets (CryptoPrice / CryptoPriceHourly / CryptoFundingRate
    # / CryptoOpenInterest / CryptoInfo, W5+W5b).
    assert len(body) == 90
    by_code = {row["dataset_code"]: row for row in body}
    assert "TaiwanStockPrice" in by_code
    assert by_code["TaiwanStockPrice"]["per_symbol"] is True


@pytest.mark.asyncio
async def test_proxy_patch_toggles_enabled(
    client, db_session, finmind_db_override,
):
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_toggle@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.patch(
        "/api/admin/finmind/datasets/TaiwanStockPrice",
        json={"enabled": True},
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is True


@pytest.mark.asyncio
async def test_proxy_patch_validates_active_source(
    client, db_session, finmind_db_override,
):
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_valid@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.patch(
        "/api/admin/finmind/datasets/TaiwanStockPrice",
        json={"active_source": "definitely_not_real"},
        headers=_auth(token),
    )
    assert r.status_code == 400
    assert "active_source must be" in r.json()["detail"]


@pytest.mark.asyncio
async def test_proxy_patch_phase_a_to_b_switch(
    client, db_session, finmind_db_override,
):
    """Headline operator action — flip Phase A → B for one dataset
    via a single PATCH. After the switch the read API metadata
    reflects the new active_source."""
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_switch@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.patch(
        "/api/admin/finmind/datasets/TaiwanStockPrice",
        json={"active_source": "twse"},
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["active_source"] == "twse"


@pytest.mark.asyncio
async def test_proxy_patch_rejects_uncovered_dataset_on_real_source(
    client, db_session, finmind_db_override,
):
    """Per-dataset gatekeeper: source='twse' is implemented but only
    covers 6 datasets. Flipping `TaiwanFuturesDaily` → twse must be
    rejected with a 400 + remediation hint, NOT silently land in the
    DB and break the next ingest."""
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_partial@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.patch(
        "/api/admin/finmind/datasets/TaiwanFuturesDaily",
        json={"active_source": "twse"},
        headers=_auth(token),
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "no handler for dataset" in detail
    assert "TaiwanFuturesDaily" in detail
    assert "twse" in detail


@pytest.mark.asyncio
async def test_proxy_patch_rejects_fully_stubbed_source(
    client, db_session, finmind_db_override,
):
    """Existing 'no connector wired up yet' check must keep working
    end-to-end. tpex/taifex/tdcc all hit `_NotWiredYetClient` →
    every flip targeting them must 400 with the source name in the
    detail message so the operator knows where to look."""
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_stub@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.patch(
        "/api/admin/finmind/datasets/TaiwanFuturesDaily",
        json={"active_source": "taifex"},
        headers=_auth(token),
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "taifex" in detail
    assert "no connector wired up yet" in detail


# ── Status + usage ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_proxy_config_returns_resolved_settings(
    client, db_session, finmind_db_override, monkeypatch,
):
    """`/config` mirrors the startup log line — surfaces resolved
    FinMind env-var settings to the AdminPage UI without needing
    shell access. Independent of DB; uses settings only."""
    # CI sets DATABASE_URL=sqlite for the main app's in-memory test
    # engine (.github/workflows/ci.yml). With FINMIND_USE_MAIN_DB=True
    # (the post-#313 default), `finmind_settings.effective_database_url`
    # would resolve to that sqlite URL and the proxy would report
    # mode='sqlite-test', which is a valid runtime mode but isn't
    # what this test is pinning. Pretend DATABASE_URL is postgres for
    # the duration of this test so the resolver exercises the Path A2
    # branch (shared-main-db + schema=finmind).
    from config import settings as main_settings

    monkeypatch.setattr(
        main_settings,
        "DATABASE_URL",
        "postgresql+asyncpg://fincept:test@localhost:5432/finceptweb",
    )

    email = "admin_fm_config@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/config", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "use_main_db", "auto_init", "effective_database_url",
        "schema_", "mode",
    }
    # Default since PR #313: FINMIND_USE_MAIN_DB=True + postgres
    # DATABASE_URL → resolved mode is `shared-main-db` with
    # `schema=finmind`. Pins that the default-default (no env vars
    # touched on the FinMind side) lands in Path A2.
    assert body["use_main_db"] is True
    assert body["mode"] == "shared-main-db"
    assert body["schema_"] == "finmind"
    assert body["auto_init"] is True  # default
    # Password masking — even sqlite URLs round-trip cleanly with no creds.
    assert "password" not in body["effective_database_url"].lower()


@pytest.mark.asyncio
async def test_proxy_config_requires_admin(client, db_session):
    """Non-admin users get 403 — config exposes the (masked) DB URL
    which is operationally sensitive."""
    email = "viewer_fm_config@test.com"
    await _register_login(client, email)
    # Login as non-admin (default role is viewer).
    r_login = await client.post(
        "/api/auth/login",
        json={"email": email, "password": "Test1234!"},
    )
    token = r_login.json()["access_token"]

    r = await client.get(
        "/api/admin/finmind/config", headers=_auth(token),
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_proxy_status_returns_collect_status_shape(
    client, db_session, finmind_db_override,
):
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_status@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/status", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    for k in (
        "alembic", "catalog", "phase1_coverage",
        "active_ingestion", "backfill", "recent_errors", "generated_at",
    ):
        assert k in body
    assert body["catalog"]["seeded"] == 90


@pytest.mark.asyncio
async def test_proxy_usage_empty_when_no_events(
    client, db_session, finmind_db_override,
):
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_usage@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/usage?days=7", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["window_days"] == 7
    assert body["by_day"] == []
    assert body["by_dataset"] == []


@pytest.mark.asyncio
async def test_proxy_usage_aggregates_recent_events(
    client, db_session, finmind_db_override,
):
    """Insert a few api_usage_events rows with varied datasets +
    timestamps, verify the rollup groups them correctly. This is the
    happy path the FinmindUsageCard chart consumes."""
    from datetime import datetime, timezone

    from finmind.models.billing import ApiUsageEvent

    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    now = datetime.now(tz=timezone.utc)
    async with SessionLocal() as s:
        s.add_all([
            ApiUsageEvent(
                ts=now,
                api_key_id=1,
                dataset_code="TaiwanStockPrice",
                endpoint="/data/TaiwanStockPrice",
                row_count=100, bytes_out=1024, latency_ms=10,
                status_code=200,
            ),
            ApiUsageEvent(
                ts=now,
                api_key_id=2,
                dataset_code="TaiwanStockPrice",
                endpoint="/data/TaiwanStockPrice",
                row_count=50, bytes_out=512, latency_ms=8,
                status_code=200,
            ),
            ApiUsageEvent(
                ts=now,
                api_key_id=1,
                dataset_code="TaiwanStockMarginPurchaseShortSale",
                endpoint="/data/TaiwanStockMarginPurchaseShortSale",
                row_count=30, bytes_out=256, latency_ms=5,
                status_code=200,
            ),
        ])
        await s.commit()

    email = "admin_fm_usage_pop@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/usage?days=7", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["window_days"] == 7

    # by_day: one entry for today with calls=3, rows=180
    assert len(body["by_day"]) == 1
    assert body["by_day"][0]["calls"] == 3
    assert body["by_day"][0]["rows"] == 180

    # by_dataset: TaiwanStockPrice should top the list (2 calls, 150 rows)
    by_dataset = {d["dataset_code"]: d for d in body["by_dataset"]}
    assert by_dataset["TaiwanStockPrice"]["calls"] == 2
    assert by_dataset["TaiwanStockPrice"]["rows"] == 150
    assert by_dataset["TaiwanStockMarginPurchaseShortSale"]["calls"] == 1


@pytest.mark.asyncio
async def test_proxy_usage_rejects_invalid_days(
    client, db_session, finmind_db_override,
):
    email = "admin_fm_usage_v@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/usage?days=999", headers=_auth(token),
    )
    assert r.status_code == 400
    assert "between 1 and 90" in r.json()["detail"]


# ── Manual run buttons ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_dataset_returns_404_for_unknown(
    client, db_session, finmind_db_override,
):
    email = "admin_fm_run404@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/datasets/TotallyMadeUp/run",
        json={},
        headers=_auth(token),
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_run_dataset_returns_503_when_no_local_table(
    client, db_session, finmind_db_override,
):
    """Realtime / unbuilt datasets have empty local_table — trigger
    must NOT proceed (would UPSERT into ''). Surface 503 cleanly."""
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_run503@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/datasets/taiwan_stock_tick_snapshot/run",
        json={},
        headers=_auth(token),
    )
    assert r.status_code == 503
    assert "destination table not yet built" in r.json()["detail"]


@pytest.mark.asyncio
async def test_run_dataset_default_window_is_last_7_days(
    client, db_session, finmind_db_override, monkeypatch,
):
    """Without start/end the trigger backfills the last 7 days —
    matches the cron's default window. Verify the runner sees those
    bounds by capturing the call args."""
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    captured: dict = {}

    async def fake_ingest_chunk(session, **kwargs):
        captured.update(kwargs)
        from finmind.ingest.runner import ChunkResult
        return ChunkResult(status="done", rows_written=5, error=None)

    import finmind.ingest.runner as _runner
    monkeypatch.setattr(_runner, "ingest_chunk", fake_ingest_chunk)

    email = "admin_fm_run_def@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/datasets/TaiwanStockPrice/run",
        json={},
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["rows_written"] == 5

    # Default window: 7 days back from today.
    from datetime import datetime, timedelta, timezone
    assert captured["range_end"] == datetime.now(timezone.utc).date()
    assert captured["range_start"] == datetime.now(timezone.utc).date() - timedelta(days=7)


@pytest.mark.asyncio
async def test_run_dataset_honors_explicit_dates_and_symbol(
    client, db_session, finmind_db_override, monkeypatch,
):
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    captured: dict = {}

    async def fake_ingest_chunk(session, **kwargs):
        captured.update(kwargs)
        from finmind.ingest.runner import ChunkResult
        return ChunkResult(status="done", rows_written=42, error=None)

    import finmind.ingest.runner as _runner
    monkeypatch.setattr(_runner, "ingest_chunk", fake_ingest_chunk)

    email = "admin_fm_run_explicit@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/datasets/TaiwanStockPrice/run",
        json={
            "symbol": "2330",
            "start_date": "2024-03-01",
            "end_date": "2024-03-31",
        },
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "2330"
    assert body["range_start"] == "2024-03-01"
    assert body["range_end"] == "2024-03-31"

    from datetime import date
    assert captured["symbol"] == "2330"
    assert captured["range_start"] == date(2024, 3, 1)
    assert captured["range_end"] == date(2024, 3, 31)


@pytest.mark.asyncio
async def test_run_due_returns_summary_when_nothing_enabled(
    client, db_session, finmind_db_override,
):
    """Default catalog state — no datasets enabled → run_due_now
    returns 0 outcomes. Endpoint must still 200 with empty summary
    so the frontend renders "0 chunks" not an error."""
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_rd_empty@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/run-due", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["done"] == 0
    assert body["failed"] == 0
    assert body["outcomes"] == []


# ── API key issuance ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_issue_key_returns_plaintext_once(
    client, db_session, finmind_db_override,
):
    """Plaintext is exposed in the POST response and never readable
    again (we only store sha256). Assert the response shape + that
    the underlying issue_key actually persisted a row."""
    SessionLocal = finmind_db_override

    email = "admin_fm_keys_issue@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/keys",
        json={"owner_email": "customer@example.com", "name": "prod"},
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["plaintext"].startswith("fck_live_")
    assert body["prefix"].startswith("fck_live_")
    assert body["owner_email"] == "customer@example.com"
    assert body["record_id"] > 0

    # Verify row landed in api_keys.
    from finmind.models.billing import ApiKey

    async with SessionLocal() as s:
        row = await s.get(ApiKey, body["record_id"])
        assert row is not None
        assert row.owner_email == "customer@example.com"
        assert row.name == "prod"
        assert row.enabled is True


@pytest.mark.asyncio
async def test_list_keys_omits_secrets(
    client, db_session, finmind_db_override,
):
    """The listing endpoint must NEVER include `plaintext` or
    `key_hash` — operator UI shows only prefix + metadata."""
    email = "admin_fm_keys_list@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    # Issue two keys first.
    for who in ("a@x.com", "b@x.com"):
        await client.post(
            "/api/admin/finmind/keys",
            json={"owner_email": who},
            headers=_auth(token),
        )

    r = await client.get(
        "/api/admin/finmind/keys", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    for item in body:
        assert "plaintext" not in item
        assert "key_hash" not in item
        assert "prefix" in item
        assert item["enabled"] is True


@pytest.mark.asyncio
async def test_revoke_key_soft_deletes(
    client, db_session, finmind_db_override,
):
    """DELETE flips enabled=false rather than removing the row —
    keeps audit trail + api_usage_events FK references valid."""
    SessionLocal = finmind_db_override

    email = "admin_fm_keys_rev@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    issued = await client.post(
        "/api/admin/finmind/keys",
        json={"owner_email": "to-revoke@example.com"},
        headers=_auth(token),
    )
    record_id = issued.json()["record_id"]

    r = await client.delete(
        f"/api/admin/finmind/keys/{record_id}", headers=_auth(token),
    )
    assert r.status_code == 204

    # Row still exists; just disabled.
    from finmind.models.billing import ApiKey

    async with SessionLocal() as s:
        row = await s.get(ApiKey, record_id)
        assert row is not None
        assert row.enabled is False

    # Listing still shows it.
    listing = await client.get(
        "/api/admin/finmind/keys", headers=_auth(token),
    )
    revoked = next(k for k in listing.json() if k["id"] == record_id)
    assert revoked["enabled"] is False


@pytest.mark.asyncio
async def test_revoke_key_404_for_unknown(
    client, db_session, finmind_db_override,
):
    email = "admin_fm_keys_404@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.delete(
        "/api/admin/finmind/keys/999999", headers=_auth(token),
    )
    assert r.status_code == 404


# ── Plans CRUD ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_plans_empty(
    client, db_session, finmind_db_override,
):
    email = "admin_plans_empty@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/plans", headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_upsert_plan_creates_then_updates(
    client, db_session, finmind_db_override,
):
    """Same PUT endpoint creates the plan first time, updates the
    second — idempotent contract the frontend relies on."""
    email = "admin_plans_upsert@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.put(
        "/api/admin/finmind/plans/pro",
        json={
            "name": "Pro",
            "price_monthly": 990,
            "quota_daily_calls": 10_000,
            "quota_daily_rows": 1_000_000,
        },
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["code"] == "pro"
    assert r.json()["quota_daily_calls"] == 10_000

    r = await client.put(
        "/api/admin/finmind/plans/pro",
        json={
            "name": "Pro (updated)",
            "price_monthly": 1990,
            "quota_daily_calls": 20_000,
            "quota_daily_rows": 2_000_000,
        },
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Pro (updated)"
    assert r.json()["quota_daily_calls"] == 20_000

    listing = await client.get(
        "/api/admin/finmind/plans", headers=_auth(token),
    )
    assert len(listing.json()) == 1


@pytest.mark.asyncio
async def test_disable_plan_soft_deletes(
    client, db_session, finmind_db_override,
):
    """DELETE flips enabled=false rather than removing the row —
    keeps subscriptions FK-valid."""
    email = "admin_plans_disable@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    await client.put(
        "/api/admin/finmind/plans/lite",
        json={"name": "Lite", "quota_daily_calls": 500, "quota_daily_rows": 50_000},
        headers=_auth(token),
    )
    r = await client.delete(
        "/api/admin/finmind/plans/lite", headers=_auth(token),
    )
    assert r.status_code == 204

    listing = await client.get(
        "/api/admin/finmind/plans", headers=_auth(token),
    )
    plan = next(p for p in listing.json() if p["code"] == "lite")
    assert plan["enabled"] is False


@pytest.mark.asyncio
async def test_disable_plan_404_for_unknown(
    client, db_session, finmind_db_override,
):
    email = "admin_plans_404@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.delete(
        "/api/admin/finmind/plans/nope", headers=_auth(token),
    )
    assert r.status_code == 404


# ── Key-to-plan linking ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_issue_key_with_plan_creates_subscription(
    client, db_session, finmind_db_override,
):
    """Headline integration: POST /keys with plan_code → backend
    creates an active Subscription and links the new ApiKey to it."""
    SessionLocal = finmind_db_override

    email = "admin_keys_plan@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    await client.put(
        "/api/admin/finmind/plans/pro",
        json={
            "name": "Pro",
            "price_monthly": 990,
            "quota_daily_calls": 10_000,
            "quota_daily_rows": 1_000_000,
        },
        headers=_auth(token),
    )

    r = await client.post(
        "/api/admin/finmind/keys",
        json={
            "owner_email": "customer@example.com",
            "plan_code": "pro",
        },
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["plan_code"] == "pro"
    assert body["subscription_id"] is not None

    listing = await client.get(
        "/api/admin/finmind/keys", headers=_auth(token),
    )
    key = next(k for k in listing.json() if k["id"] == body["record_id"])
    assert key["plan_code"] == "pro"
    assert key["subscription_id"] == body["subscription_id"]

    from finmind.models.billing import Subscription
    async with SessionLocal() as s:
        sub = await s.get(Subscription, body["subscription_id"])
        assert sub is not None
        assert sub.owner_email == "customer@example.com"
        assert sub.plan_code == "pro"
        assert sub.status == "active"


@pytest.mark.asyncio
async def test_issue_key_with_unknown_plan_returns_400(
    client, db_session, finmind_db_override,
):
    """Unknown plan_code → 400. The previous silent fallback (issue
    free-tier key, surface plan_code=None) masked typos — the operator
    typing a plan_code clearly intended that plan, so the mismatch
    should fail loudly."""
    email = "admin_keys_unknownplan@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/keys",
        json={
            "owner_email": "customer@example.com",
            "plan_code": "definitely_not_a_plan",
        },
        headers=_auth(token),
    )
    assert r.status_code == 400
    assert "definitely_not_a_plan" in r.json()["detail"]
    assert "does not exist" in r.json()["detail"]


@pytest.mark.asyncio
async def test_issue_key_with_disabled_plan_returns_400(
    client, db_session, finmind_db_override,
):
    """Plan exists but enabled=false → 400 with a different hint
    pointing to re-enabling vs. picking another plan."""
    email = "admin_keys_disabled@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    await client.put(
        "/api/admin/finmind/plans/sunset",
        json={"name": "Sunset", "quota_daily_calls": 100, "quota_daily_rows": 1_000},
        headers=_auth(token),
    )
    await client.delete(
        "/api/admin/finmind/plans/sunset", headers=_auth(token),
    )

    r = await client.post(
        "/api/admin/finmind/keys",
        json={"owner_email": "x@x.com", "plan_code": "sunset"},
        headers=_auth(token),
    )
    assert r.status_code == 400
    assert "sunset" in r.json()["detail"]
    assert "disabled" in r.json()["detail"]


@pytest.mark.asyncio
async def test_issue_key_without_plan_code_still_works(
    client, db_session, finmind_db_override,
):
    """Omitting plan_code (free-tier intent) keeps working. Confirms
    the new strict validation only fires when plan_code is set."""
    email = "admin_keys_noplan@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/keys",
        json={"owner_email": "free@example.com"},
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["plan_code"] is None
    assert body["subscription_id"] is None
    assert body["plaintext"].startswith("fck_live_")


@pytest.mark.asyncio
async def test_run_due_invokes_runner_for_enabled_datasets(
    client, db_session, finmind_db_override, monkeypatch,
):
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    # Enable one dataset so run_due_now has work.
    from finmind.models.dataset_source import DatasetSource as DS
    async with SessionLocal() as s:
        row = await s.get(DS, "TaiwanStockTotalMarginPurchaseShortSale")
        row.enabled = True
        await s.commit()

    # Mock ingest_chunk so we don't actually call FinMind. Patch the
    # binding inside `finmind.scheduler.runner` (the consumer), not
    # `finmind.ingest.runner` (the source) — `scheduler.runner` does
    # `from finmind.ingest.runner import ingest_chunk` at module load,
    # so once that module has been imported (e.g. by a prior test in
    # the same process) patching the source has no effect on the
    # already-bound name in the scheduler.
    async def fake_ingest_chunk(session, **kwargs):
        from finmind.ingest.runner import ChunkResult
        return ChunkResult(status="done", rows_written=3, error=None)

    import finmind.scheduler.runner as _sched_runner
    monkeypatch.setattr(_sched_runner, "ingest_chunk", fake_ingest_chunk)

    email = "admin_fm_rd_run@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/run-due", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["done"] == 1
    assert body["rows_written"] == 3
    assert body["outcomes"][0]["dataset_code"] == "TaiwanStockTotalMarginPurchaseShortSale"


# ── Status endpoint: clean 503 when DB is unreachable ─────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/finmind/status",
        "/api/admin/finmind/datasets",
        "/api/admin/finmind/plans",
        "/api/admin/finmind/keys",
        "/api/admin/finmind/usage",
    ],
)
async def test_read_endpoints_return_503_when_finmind_db_unreachable(
    client, db_session, path,
):
    """Every read endpoint that the AdminPage auto-fires on mount
    must convert a ConnectionRefusedError on the underlying session
    into a clean 503. Otherwise a fresh deployment (postgres_finmind
    not up yet) drops a generic 500 into multiple cards on the page."""
    from finmind.db.session import get_finmind_db
    from main import app

    class _FailingSession:
        async def execute(self, *args, **kwargs):
            raise ConnectionRefusedError(
                "[Errno 111] Connection refused"
            )

    async def _override():
        yield _FailingSession()

    app.dependency_overrides[get_finmind_db] = _override
    try:
        email = f"admin_fm_unreachable_{path.replace('/', '_')}@test.com"
        await _register_login(client, email)
        token = await _promote_to_admin(db_session, email, client)

        r = await client.get(path, headers=_auth(token))
        assert r.status_code == 503, (
            f"{path} should 503 on DB-down, got {r.status_code}"
        )
        body = r.json()
        assert "ConnectionRefusedError" in body["detail"]
        assert "Setup checklist" in body["detail"]
    finally:
        app.dependency_overrides.pop(get_finmind_db, None)


@pytest.mark.asyncio
async def test_status_returns_200_when_db_reachable(
    client, db_session, finmind_db_override,
):
    """Sanity check the happy path still works after adding the probe."""
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_status_ok@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/status", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert "alembic" in body
    assert "catalog" in body
