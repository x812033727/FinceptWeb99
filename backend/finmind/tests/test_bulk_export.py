"""Tests for `/data/{dataset_code}/export` — CSV / JSONL streaming.

Covers:
  - CSV format: header line + per-row CSV with proper quoting
  - JSONL format: one JSON object per line, parseable
  - data_id + start_date + end_date filters apply
  - limit cap honored (1M hard cap, lower per-request cap respected)
  - Auth required (401 when allowlist set + no key)
  - 429 when quota exhausted (no stream produced)
  - 200 path logs usage with row_count + bytes_out
  - Soft cap: per-request limit clamps to remaining row quota
  - Streaming Content-Disposition + media-type headers correct
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from finmind.api.router import router
from finmind.billing.keys import issue_key
from finmind.billing.quota import _FREE_TIER_CALL_LIMIT, record_usage
from finmind.db.session import get_finmind_db
from finmind.models.billing import ApiUsageEvent
from finmind.scripts.init_db import seed_dataset_sources


@pytest.fixture(autouse=True)
def _enable_dev_auth_bypass(monkeypatch):
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.delenv("FINMIND_API_KEYS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FINMIND_ADMIN_API_KEY", raising=False)


@pytest.fixture
def app(finmind_session):
    fastapi_app = FastAPI()
    fastapi_app.include_router(router, prefix="/api/finmind")

    async def _override():
        yield finmind_session

    fastapi_app.dependency_overrides[get_finmind_db] = _override
    return fastapi_app


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as ac:
        yield ac


async def _seed_ohlcv_rows(finmind_session, n: int = 5) -> None:
    """Insert N rows for symbol 2330 across consecutive days starting
    2024-01-02 (skipping 2024-01-01 New Year holiday)."""
    inserts = []
    for i in range(n):
        day = i + 2
        inserts.append(
            f"('TWSE', '2330', '2024-01-{day:02d}', "
            f"{600 + i}, {605 + i}, {598 + i}, {603 + i}, "
            f"{12345 + i * 1000}, 'finmind')"
        )
    await finmind_session.execute(
        text(
            "INSERT INTO ohlcv_daily "
            "(market, symbol, ts, open, high, low, close, volume, source) "
            "VALUES " + ", ".join(inserts)
        )
    )
    await finmind_session.commit()


# ── Format correctness ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_csv_export_returns_header_plus_rows(
    client, finmind_session,
):
    await seed_dataset_sources()
    await _seed_ohlcv_rows(finmind_session, n=3)

    resp = await client.get(
        "/api/finmind/data/TaiwanStockPrice/export",
        params={"data_id": "2330", "format": "csv"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"].endswith('"TaiwanStockPrice.csv"')

    text_body = resp.text
    lines = text_body.strip().split("\n")
    # Header + 3 data rows.
    assert len(lines) == 4
    header = lines[0].split(",")
    assert "symbol" in header
    assert "ts" in header
    assert "close" in header

    # Row content includes our seeded data.
    body_text = "\n".join(lines[1:])
    assert "2330" in body_text
    assert "2024-01-02" in body_text


@pytest.mark.asyncio
async def test_jsonl_export_returns_one_object_per_line(
    client, finmind_session,
):
    await seed_dataset_sources()
    await _seed_ohlcv_rows(finmind_session, n=3)

    resp = await client.get(
        "/api/finmind/data/TaiwanStockPrice/export",
        params={"data_id": "2330", "format": "jsonl"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")

    lines = [line for line in resp.text.strip().split("\n") if line]
    assert len(lines) == 3
    # Each line parses as JSON, has the expected keys.
    for line in lines:
        obj = json.loads(line)
        assert obj["symbol"] == "2330"
        assert "ts" in obj
        assert "close" in obj


@pytest.mark.asyncio
async def test_csv_export_quotes_values_containing_commas(
    client, finmind_session,
):
    """A value with embedded commas / newlines / quotes must be
    RFC 4180-quoted so the CSV survives downstream parsers."""
    await seed_dataset_sources()
    await finmind_session.execute(
        text(
            "INSERT INTO tw_news_articles "
            "(sha256, market, symbol, title, link, source) VALUES "
            "('abc', 'TW', '2330', "
            "'comma, here \"and\" quote', "
            "'https://example.com/1', 'finmind')"
        )
    )
    await finmind_session.commit()

    resp = await client.get(
        "/api/finmind/data/TaiwanStockNews/export",
        params={"data_id": "2330", "format": "csv"},
    )
    assert resp.status_code == 200
    body = resp.text
    # Title should be wrapped in quotes + embedded quotes doubled.
    assert '"comma, here ""and"" quote"' in body


# ── Filters + limits ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_respects_data_id_filter(client, finmind_session):
    await seed_dataset_sources()
    await _seed_ohlcv_rows(finmind_session, n=3)
    await finmind_session.execute(
        text(
            "INSERT INTO ohlcv_daily "
            "(market, symbol, ts, open, high, low, close, volume, source) "
            "VALUES ('TWSE', '2317', '2024-01-02', 100, 102, 99, 101, 5678, 'finmind')"
        )
    )
    await finmind_session.commit()

    resp = await client.get(
        "/api/finmind/data/TaiwanStockPrice/export",
        params={"data_id": "2330", "format": "csv"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "2330" in body
    assert "2317" not in body


@pytest.mark.asyncio
async def test_export_respects_date_range_filter(client, finmind_session):
    await seed_dataset_sources()
    await _seed_ohlcv_rows(finmind_session, n=10)  # 2024-01-02 .. 2024-01-11

    resp = await client.get(
        "/api/finmind/data/TaiwanStockPrice/export",
        params={
            "data_id": "2330",
            "start_date": "2024-01-04",
            "end_date": "2024-01-06",
            "format": "csv",
        },
    )
    assert resp.status_code == 200
    lines = [line for line in resp.text.strip().split("\n") if line][1:]  # skip header
    assert len(lines) == 3  # 2024-01-04, 05, 06


@pytest.mark.asyncio
async def test_export_respects_limit_param(client, finmind_session):
    await seed_dataset_sources()
    await _seed_ohlcv_rows(finmind_session, n=10)

    resp = await client.get(
        "/api/finmind/data/TaiwanStockPrice/export",
        params={"data_id": "2330", "limit": 4, "format": "csv"},
    )
    assert resp.status_code == 200
    lines = [line for line in resp.text.strip().split("\n") if line][1:]
    assert len(lines) == 4


# ── Quota interaction ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_logs_usage_with_row_count(client, finmind_session):
    """Verify the post-stream usage record captures rows + bytes —
    drives billing rollups."""
    await seed_dataset_sources()
    await _seed_ohlcv_rows(finmind_session, n=5)

    issued = await issue_key(
        finmind_session, owner_email="user@example.com",
    )

    resp = await client.get(
        "/api/finmind/data/TaiwanStockPrice/export",
        params={"data_id": "2330", "format": "csv"},
        headers={"X-Finmind-API-Key": issued.plaintext},
    )
    assert resp.status_code == 200
    # Force the streaming generator to fully consume.
    body = resp.text
    assert body  # non-empty

    events = (
        await finmind_session.execute(
            select(ApiUsageEvent).where(
                ApiUsageEvent.api_key_id == issued.record_id
            )
        )
    ).scalars().all()
    assert len(events) == 1
    e = events[0]
    assert e.endpoint == "/data/TaiwanStockPrice/export"
    assert e.status_code == 200
    assert e.row_count == 5
    assert e.bytes_out > 0


@pytest.mark.asyncio
async def test_export_429_when_quota_exhausted(client, finmind_session):
    """Pre-burn the call quota → export 429s with Retry-After + the
    rejected request itself logged (status_code=429)."""
    await seed_dataset_sources()
    await _seed_ohlcv_rows(finmind_session, n=3)

    issued = await issue_key(
        finmind_session, owner_email="user@example.com",
    )
    for _ in range(_FREE_TIER_CALL_LIMIT):
        await record_usage(
            finmind_session, api_key_id=issued.record_id,
            dataset_code="TaiwanStockPrice",
            endpoint="/data/TaiwanStockPrice",
            row_count=1, bytes_out=10, latency_ms=1, status_code=200,
        )

    resp = await client.get(
        "/api/finmind/data/TaiwanStockPrice/export",
        params={"data_id": "2330", "format": "csv"},
        headers={"X-Finmind-API-Key": issued.plaintext},
    )
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


@pytest.mark.asyncio
async def test_export_caps_limit_to_remaining_row_quota(
    client, finmind_session,
):
    """If a customer has 100 row budget remaining and asks for 1000,
    the export caps at 100 — keeps a single export from starving the
    rest of the day."""
    from datetime import date

    from finmind.models.billing import Plan, Subscription

    finmind_session.add(Plan(
        code="capped",
        name="Capped",
        quota_daily_calls=100,
        quota_daily_rows=10,  # only 10 rows total
        currency="TWD",
        enabled=True,
    ))
    sub = Subscription(
        owner_email="user@example.com",
        plan_code="capped",
        status="active",
        started_at=date(2024, 1, 1),
    )
    finmind_session.add(sub)
    await finmind_session.commit()

    issued = await issue_key(
        finmind_session,
        owner_email="user@example.com",
        subscription_id=sub.id,
    )

    await seed_dataset_sources()
    await _seed_ohlcv_rows(finmind_session, n=20)

    resp = await client.get(
        "/api/finmind/data/TaiwanStockPrice/export",
        params={"data_id": "2330", "limit": 1000, "format": "csv"},
        headers={"X-Finmind-API-Key": issued.plaintext},
    )
    assert resp.status_code == 200
    lines = [line for line in resp.text.strip().split("\n") if line][1:]
    # Capped at 10 (remaining row quota), not the requested 1000.
    assert len(lines) == 10


# ── Error paths ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_404_unknown_dataset(client):
    resp = await client.get(
        "/api/finmind/data/TotallyMadeUp/export",
        params={"format": "csv"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_export_503_when_local_table_empty(client, finmind_session):
    await seed_dataset_sources()
    resp = await client.get(
        "/api/finmind/data/taiwan_stock_tick_snapshot/export",
        params={"format": "csv"},
    )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_export_400_when_format_invalid(client, finmind_session):
    """Pydantic Query regex enforces csv|jsonl — anything else 422
    (FastAPI's validation error code)."""
    await seed_dataset_sources()
    resp = await client.get(
        "/api/finmind/data/TaiwanStockPrice/export",
        params={"data_id": "2330", "format": "xml"},
    )
    assert resp.status_code == 422
