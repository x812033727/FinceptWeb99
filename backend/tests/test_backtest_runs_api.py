"""C3 — persisted backtest runs: save / list / get / delete / compare.

Covers:
* save=true persists a run and returns run_id; save omitted persists
  nothing.
* list / get / delete are strictly user-scoped (another analyst's run
  404s / never appears in the list).
* trades are capped at 500 with the truncation flag in config.
* compare normalisation math (each curve → 100 at its own first bar,
  union-of-dates alignment) and its guard rails (≤4 ids, ownership).
"""
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import AsyncMock, patch

from models.backtest_run import BacktestRun
from models.user import User, UserRole
from services.backtest_run_service import cap_trades, normalise_curve


# ── helpers ───────────────────────────────────────────────────────

async def _analyst_token(client: AsyncClient, db: AsyncSession, email: str) -> str:
    await client.post("/api/auth/register", json={"email": email, "password": "Pass99!!"})
    user = (await db.execute(select(User).where(User.email == email))).scalar_one()
    user.role = UserRole.analyst
    await db.commit()
    r = await client.post("/api/auth/login", json={"email": email, "password": "Pass99!!"})
    return r.json()["access_token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _fake_result(
    equity: list[dict] | None = None,
    trades: list[dict] | None = None,
    metrics: dict | None = None,
) -> dict:
    return {
        "status": "completed",
        "equity_curve": equity or [
            {"date": "2024-01-01", "value": 100000.0},
            {"date": "2024-01-02", "value": 101000.0},
            {"date": "2024-01-03", "value": 99500.0},
        ],
        "trades": trades if trades is not None else [
            {"date": "2024-01-02", "symbol": "AAPL", "side": "buy",
             "quantity": 10, "price": 100.0},
        ],
        "metrics": metrics or {
            "total_return_pct": -0.5, "sharpe_ratio": 0.1,
            "max_drawdown_pct": -1.49, "total_trades": 1,
            "final_value": 99500.0,
        },
    }


_BODY = {
    "symbols": ["AAPL"], "markets": ["US"],
    "strategy": "sma_crossover", "params": {"fast": 5, "slow": 20},
    "start_date": "2024-01-01", "end_date": "2024-06-30",
    "initial_capital": 100000,
}


async def _save_run(
    client: AsyncClient, token: str, *, name: str | None = None,
    result: dict | None = None,
) -> dict:
    with patch("services.analytics_service.run_backtest_analysis",
               new_callable=AsyncMock) as mock_run:
        mock_run.return_value = result or _fake_result()
        r = await client.post(
            "/api/analytics/backtest",
            json={**_BODY, "save": True, **({"name": name} if name else {})},
            headers=_headers(token),
        )
    assert r.status_code == 200, r.text
    return r.json()


async def _run_count(db: AsyncSession) -> int:
    return (
        await db.execute(select(func.count()).select_from(BacktestRun))
    ).scalar_one()


# ── save ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backtest_save_persists_and_returns_run_id(
    client: AsyncClient, db_session: AsyncSession,
):
    token = await _analyst_token(client, db_session, "c3_save@example.com")
    data = await _save_run(client, token, name="我的第一次回測")

    assert data["status"] == "completed"
    assert data["run_id"]
    row = await db_session.get(BacktestRun, uuid.UUID(data["run_id"]))
    assert row is not None
    assert row.name == "我的第一次回測"
    assert row.strategy == "sma_crossover"
    assert row.params == {"fast": 5, "slow": 20}
    assert row.config["symbols"] == ["AAPL"]
    assert row.config["start_date"] == "2024-01-01"
    assert row.config["trades_truncated"] is False
    assert row.metrics["final_value"] == 99500.0
    assert len(row.equity_curve) == 3


@pytest.mark.asyncio
async def test_backtest_without_save_persists_nothing(
    client: AsyncClient, db_session: AsyncSession,
):
    token = await _analyst_token(client, db_session, "c3_nosave@example.com")
    with patch("services.analytics_service.run_backtest_analysis",
               new_callable=AsyncMock) as mock_run:
        mock_run.return_value = _fake_result()
        r = await client.post("/api/analytics/backtest", json=_BODY,
                              headers=_headers(token))
    assert r.status_code == 200
    assert r.json().get("run_id") is None
    assert await _run_count(db_session) == 0


@pytest.mark.asyncio
async def test_backtest_save_failed_run_persists_nothing(
    client: AsyncClient, db_session: AsyncSession,
):
    token = await _analyst_token(client, db_session, "c3_savefail@example.com")
    with patch("services.analytics_service.run_backtest_analysis",
               new_callable=AsyncMock) as mock_run:
        mock_run.return_value = {"status": "failed", "error": "No price data"}
        r = await client.post("/api/analytics/backtest",
                              json={**_BODY, "save": True, "name": "x"},
                              headers=_headers(token))
    assert r.status_code == 200
    assert r.json().get("run_id") is None
    assert await _run_count(db_session) == 0


@pytest.mark.asyncio
async def test_backtest_save_caps_trades_at_500_with_flag(
    client: AsyncClient, db_session: AsyncSession,
):
    token = await _analyst_token(client, db_session, "c3_trunc@example.com")
    many_trades = [
        {"date": f"2024-{(i % 12) + 1:02d}-01", "symbol": "AAPL",
         "side": "buy" if i % 2 == 0 else "sell", "quantity": 1,
         "price": 100.0 + i}
        for i in range(650)
    ]
    data = await _save_run(
        client, token,
        result=_fake_result(trades=many_trades,
                            metrics={"total_trades": 650, "final_value": 1.0}),
    )
    row = await db_session.get(BacktestRun, uuid.UUID(data["run_id"]))
    assert len(row.trades) == 500
    # Kept the LAST 500 — the newest trades survive the cap.
    assert row.trades[-1]["price"] == 100.0 + 649
    assert row.trades[0]["price"] == 100.0 + 150
    assert row.config["trades_truncated"] is True
    assert row.metrics["total_trades"] == 650   # full count preserved


def test_cap_trades_marks_engine_side_truncation():
    """The engine already trims to its last 200; metrics.total_trades
    above the returned length must still mark truncation."""
    trades = [{"i": i} for i in range(200)]
    capped, truncated = cap_trades(trades, total_trades=350)
    assert len(capped) == 200
    assert truncated is True
    capped, truncated = cap_trades(trades, total_trades=200)
    assert truncated is False
    assert cap_trades(None, total_trades=None) == (None, False)


# ── list / get / delete + user scoping ────────────────────────────

@pytest.mark.asyncio
async def test_backtest_runs_require_auth(client: AsyncClient):
    assert (await client.get("/api/analytics/backtest-runs")).status_code == 401
    rid = uuid.uuid4()
    assert (await client.get(f"/api/analytics/backtest-runs/{rid}")).status_code == 401
    assert (await client.delete(f"/api/analytics/backtest-runs/{rid}")).status_code == 401
    assert (
        await client.get(f"/api/analytics/backtest-runs/compare?ids={rid}")
    ).status_code == 401


@pytest.mark.asyncio
async def test_list_runs_is_user_scoped_and_paginated(
    client: AsyncClient, db_session: AsyncSession,
):
    token_a = await _analyst_token(client, db_session, "c3_lista@example.com")
    token_b = await _analyst_token(client, db_session, "c3_listb@example.com")

    ids = []
    for i in range(3):
        data = await _save_run(client, token_a, name=f"run-{i}")
        ids.append(data["run_id"])
    await _save_run(client, token_b, name="other-user")

    r = await client.get("/api/analytics/backtest-runs?limit=2&offset=0",
                         headers=_headers(token_a))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert body["limit"] == 2 and body["offset"] == 0
    assert len(body["items"]) == 2
    # Newest first, and none of B's runs appear.
    assert body["items"][0]["name"] == "run-2"
    assert all(item["id"] in ids for item in body["items"])
    # Summary rows stay light — no equity curve / trades payload.
    assert "equity_curve" not in body["items"][0]
    assert "trades" not in body["items"][0]

    r2 = await client.get("/api/analytics/backtest-runs?limit=2&offset=2",
                          headers=_headers(token_a))
    assert [i["name"] for i in r2.json()["items"]] == ["run-0"]


@pytest.mark.asyncio
async def test_get_run_detail_and_cross_user_404(
    client: AsyncClient, db_session: AsyncSession,
):
    token_a = await _analyst_token(client, db_session, "c3_geta@example.com")
    token_b = await _analyst_token(client, db_session, "c3_getb@example.com")
    data = await _save_run(client, token_a, name="detail-run")
    rid = data["run_id"]

    r = await client.get(f"/api/analytics/backtest-runs/{rid}",
                         headers=_headers(token_a))
    assert r.status_code == 200
    detail = r.json()
    assert detail["name"] == "detail-run"
    assert detail["params"] == {"fast": 5, "slow": 20}
    assert len(detail["equity_curve"]) == 3
    assert detail["trades"] and detail["trades"][0]["symbol"] == "AAPL"

    # Another user's fetch is indistinguishable from a missing run.
    r = await client.get(f"/api/analytics/backtest-runs/{rid}",
                         headers=_headers(token_b))
    assert r.status_code == 404
    r = await client.get(f"/api/analytics/backtest-runs/{uuid.uuid4()}",
                         headers=_headers(token_a))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_run_user_scoped(
    client: AsyncClient, db_session: AsyncSession,
):
    token_a = await _analyst_token(client, db_session, "c3_dela@example.com")
    token_b = await _analyst_token(client, db_session, "c3_delb@example.com")
    rid = (await _save_run(client, token_a, name="to-delete"))["run_id"]

    # B cannot delete A's run — and the row survives.
    r = await client.delete(f"/api/analytics/backtest-runs/{rid}",
                            headers=_headers(token_b))
    assert r.status_code == 404
    assert await _run_count(db_session) == 1

    r = await client.delete(f"/api/analytics/backtest-runs/{rid}",
                            headers=_headers(token_a))
    assert r.status_code == 200
    assert await _run_count(db_session) == 0
    # Second delete → 404.
    r = await client.delete(f"/api/analytics/backtest-runs/{rid}",
                            headers=_headers(token_a))
    assert r.status_code == 404


# ── compare ───────────────────────────────────────────────────────

def test_normalise_curve_math():
    curve = [
        {"date": "2024-01-01", "value": 50000.0},
        {"date": "2024-01-02", "value": 55000.0},
        {"date": "2024-01-03", "value": 45000.0},
    ]
    norm = normalise_curve(curve)
    assert norm["2024-01-01"] == 100.0
    assert norm["2024-01-02"] == 110.0
    assert norm["2024-01-03"] == 90.0
    # Degenerate zero-start curve → all None, never a ZeroDivisionError.
    assert normalise_curve([{"date": "d", "value": 0}]) == {"d": None}
    assert normalise_curve([]) == {}


@pytest.mark.asyncio
async def test_compare_normalises_and_aligns(
    client: AsyncClient, db_session: AsyncSession,
):
    token = await _analyst_token(client, db_session, "c3_cmp@example.com")

    # Run A: starts at 100k on 01-01. Run B: different capital base
    # (50k) and a shifted window — exercises both normalisation and
    # union-of-dates alignment.
    rid_a = (await _save_run(client, token, name="A", result=_fake_result(
        equity=[
            {"date": "2024-01-01", "value": 100000.0},
            {"date": "2024-01-02", "value": 110000.0},
            {"date": "2024-01-03", "value": 120000.0},
        ],
    )))["run_id"]
    rid_b = (await _save_run(client, token, name="B", result=_fake_result(
        equity=[
            {"date": "2024-01-02", "value": 50000.0},
            {"date": "2024-01-03", "value": 45000.0},
            {"date": "2024-01-04", "value": 55000.0},
        ],
    )))["run_id"]

    r = await client.get(
        f"/api/analytics/backtest-runs/compare?ids={rid_a},{rid_b}",
        headers=_headers(token),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["dates"] == ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]
    by_name = {run["name"]: run for run in data["runs"]}
    # Each run normalised to 100 at ITS OWN first bar despite the
    # different capital bases (100k vs 50k).
    assert by_name["A"]["values"] == [100.0, 110.0, 120.0, None]
    assert by_name["B"]["values"] == [None, 100.0, 90.0, 110.0]
    # Output order follows the requested id order; metrics ride along.
    assert data["runs"][0]["id"] == rid_a
    assert by_name["A"]["metrics"]["final_value"] == 99500.0


@pytest.mark.asyncio
async def test_compare_guard_rails(
    client: AsyncClient, db_session: AsyncSession,
):
    token = await _analyst_token(client, db_session, "c3_cmpguard@example.com")
    token_b = await _analyst_token(client, db_session, "c3_cmpother@example.com")
    rid = (await _save_run(client, token, name="mine"))["run_id"]

    # >4 ids → 400
    five = ",".join(str(uuid.uuid4()) for _ in range(5))
    r = await client.get(f"/api/analytics/backtest-runs/compare?ids={five}",
                         headers=_headers(token))
    assert r.status_code == 400

    # non-UUID garbage → 400
    r = await client.get("/api/analytics/backtest-runs/compare?ids=abc,def",
                         headers=_headers(token))
    assert r.status_code == 400

    # duplicate ids → 400
    r = await client.get(f"/api/analytics/backtest-runs/compare?ids={rid},{rid}",
                         headers=_headers(token))
    assert r.status_code == 400

    # someone else's run in the set → blanket 404
    r = await client.get(f"/api/analytics/backtest-runs/compare?ids={rid}",
                         headers=_headers(token_b))
    assert r.status_code == 404

    # own single run works (self-comparison view)
    r = await client.get(f"/api/analytics/backtest-runs/compare?ids={rid}",
                         headers=_headers(token))
    assert r.status_code == 200
    assert len(r.json()["runs"]) == 1
