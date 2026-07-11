"""
B1 個股 AI 研究報告 API tests.

Covers: auth gating, daily-quota enforcement (shared with /chat),
context assembly wiring (right symbol/market, LLM stream mocked),
persistence after the stream completes, the missing-API-key guard,
and user scoping on the list/get endpoints.
"""
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.stock_report import StockReport
from models.user import User
from services.stock_report_service import parse_report_sections

_REPORT_MD = (
    "## 摘要\n台積電基本面穩健。\n\n"
    "## 基本面\n本益比 18 倍,月營收年增 +25%。\n\n"
    "## 籌碼與資金\n外資 5 日買超。\n\n"
    "## 技術面\nRSI 62,站上 20 日均線。\n\n"
    "## 風險\n- 匯率\n- 地緣政治\n- 庫存調整\n\n"
    "## 結論\n偏多觀察。\n**本報告由 AI 產生,僅供研究參考,非投資建議。**"
)

_FAKE_CTX = {
    "market": "TW", "focus_symbols": ["2330"],
    "focus_briefs": [{"symbol": "2330", "name_zh": "台積電"}],
    "short_term_signals": {}, "per_symbol_news_sentiment": {},
    "corporate_announcements": {"market": [], "per_symbol": {}},
    "broker_concentration": [], "errors": [],
}


# ── helpers ────────────────────────────────────────────────────────

async def _register_login(client: AsyncClient, email: str) -> str:
    await client.post("/api/auth/register", json={"email": email, "password": "Pass99!!"})
    r = await client.post("/api/auth/login", json={"email": email, "password": "Pass99!!"})
    return r.json()["access_token"]


async def _user_id(db: AsyncSession, email: str) -> str:
    result = await db.execute(select(User).where(User.email == email))
    return str(result.scalar_one().id)


def _delta_stream(chunks: list[str], with_usage: bool = True):
    """Async-generator factory that mimics `stream_chat` output."""
    async def gen(*_a, **_kw):
        for c in chunks:
            yield {"type": "delta", "text": c}
        if with_usage:
            yield {"type": "usage", "prompt_tokens": 100, "completion_tokens": 50}
    return gen


def _ctx_patch():
    return patch(
        "services.stock_report_service.assemble_report_context",
        new=AsyncMock(return_value=_FAKE_CTX),
    )


# ── generation endpoint ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_requires_auth(client: AsyncClient):
    r = await client.post("/api/ai/stock-report/TW/2330")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_generate_rejects_unknown_market(client: AsyncClient):
    tok = await _register_login(client, "sr_bad_market@test.com")
    r = await client.post(
        "/api/ai/stock-report/CRYPTO/BTC",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 400
    assert "market" in r.json()["detail"]


@pytest.mark.asyncio
async def test_generate_rejects_bad_symbol(client: AsyncClient):
    tok = await _register_login(client, "sr_bad_symbol@test.com")
    r = await client.post(
        "/api/ai/stock-report/TW/DROP%20TABLE",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_generate_enforces_daily_quota(client: AsyncClient, mock_redis):
    """Same Redis counter as /chat — a viewer past 5/day gets 429 and
    the stream never starts."""
    tok = await _register_login(client, "sr_quota@test.com")
    mock_redis.incr.return_value = 999  # way past the viewer limit
    with patch(
        "services.stock_report_service.assemble_report_context",
        new=AsyncMock(),
    ) as asm:
        r = await client.post(
            "/api/ai/stock-report/TW/2330",
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 429
    assert "quota" in r.json()["detail"].lower()
    asm.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_calls_context_assembly_with_symbol_and_market(
    client: AsyncClient,
):
    tok = await _register_login(client, "sr_ctx_wiring@test.com")
    with _ctx_patch() as asm, patch(
        "api.ai_agents.stock_report.stream_chat",
        side_effect=_delta_stream(["ok"]),
    ):
        r = await client.post(
            "/api/ai/stock-report/tw/2330",  # lowercase path → uppercased
            headers={"Authorization": f"Bearer {tok}"},
        )
        _ = r.text  # drain the stream
    assert r.status_code == 200
    asm.assert_awaited_once()
    kwargs = asm.await_args.kwargs
    assert kwargs["market"] == "TW"
    assert kwargs["symbol"] == "2330"


@pytest.mark.asyncio
async def test_stream_yields_deltas_and_persists_report(
    client: AsyncClient, db_session: AsyncSession,
):
    email = "sr_persist@test.com"
    tok = await _register_login(client, email)
    uid = await _user_id(db_session, email)

    chunks = [_REPORT_MD[:40], _REPORT_MD[40:]]
    with _ctx_patch(), patch(
        "api.ai_agents.stock_report.stream_chat",
        side_effect=_delta_stream(chunks),
    ):
        r = await client.post(
            "/api/ai/stock-report/TW/2330",
            headers={"Authorization": f"Bearer {tok}"},
        )
        body = r.text

    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    # Stage events, deltas, done marker, terminator all present.
    assert '"stage": "context"' in body
    assert '"stage": "generating"' in body
    assert "## 摘要" in body
    assert '"report_id"' in body
    assert "data: [DONE]" in body

    rows = (await db_session.scalars(select(StockReport))).all()
    assert len(rows) == 1
    row = rows[0]
    assert str(row.user_id) == uid
    assert row.symbol == "2330"
    assert row.market == "TW"
    assert row.content_md == _REPORT_MD
    assert row.model  # provider default resolved to a non-empty label
    assert row.sections is not None
    assert set(row.sections) == {"摘要", "基本面", "籌碼與資金", "技術面", "風險", "結論"}


@pytest.mark.asyncio
async def test_stream_error_refunds_and_persists_nothing(
    client: AsyncClient, db_session: AsyncSession,
):
    tok = await _register_login(client, "sr_stream_err@test.com")

    async def boom(*_a, **_kw):
        raise RuntimeError("provider exploded")
        yield  # pragma: no cover

    with _ctx_patch(), \
         patch("api.ai_agents.stock_report.stream_chat", side_effect=boom), \
         patch("api.ai_agents.stock_report._refund_quota",
               new_callable=AsyncMock) as refund:
        r = await client.post(
            "/api/ai/stock-report/TW/2330",
            headers={"Authorization": f"Bearer {tok}"},
        )
        body = r.text
    assert r.status_code == 200
    assert "provider exploded" in body
    refund.assert_awaited_once()
    rows = (await db_session.scalars(select(StockReport))).all()
    assert rows == []


@pytest.mark.asyncio
async def test_missing_api_key_placeholder_not_archived(
    client: AsyncClient, db_session: AsyncSession,
):
    """Providers with no key stream a placeholder delta instead of an
    error — the endpoint must convert it to an SSE error, refund the
    quota, and NOT save it as a report."""
    tok = await _register_login(client, "sr_no_key@test.com")
    with _ctx_patch(), \
         patch("api.ai_agents.stock_report.stream_chat",
               side_effect=_delta_stream(["[OpenAI API key not configured]"],
                                         with_usage=False)), \
         patch("api.ai_agents.stock_report._refund_quota",
               new_callable=AsyncMock) as refund:
        r = await client.post(
            "/api/ai/stock-report/TW/2330",
            headers={"Authorization": f"Bearer {tok}"},
        )
        body = r.text
    assert r.status_code == 200
    assert '"error"' in body
    assert "API key not configured" in body
    refund.assert_awaited_once()
    rows = (await db_session.scalars(select(StockReport))).all()
    assert rows == []


# ── list / get endpoints ───────────────────────────────────────────

async def _seed_report(
    db: AsyncSession, *, user_id: str, symbol: str = "2330",
    market: str = "TW", content: str = _REPORT_MD,
) -> StockReport:
    import uuid as _uuid
    row = StockReport(
        user_id=_uuid.UUID(user_id), symbol=symbol, market=market,
        content_md=content, model="gpt-4o-mini",
        sections=parse_report_sections(content),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_list_reports_scoped_to_user_with_filters(
    client: AsyncClient, db_session: AsyncSession,
):
    email_a, email_b = "sr_list_a@test.com", "sr_list_b@test.com"
    tok_a = await _register_login(client, email_a)
    await _register_login(client, email_b)
    uid_a = await _user_id(db_session, email_a)
    uid_b = await _user_id(db_session, email_b)

    await _seed_report(db_session, user_id=uid_a, symbol="2330", market="TW")
    await _seed_report(db_session, user_id=uid_a, symbol="AAPL", market="US")
    await _seed_report(db_session, user_id=uid_b, symbol="2330", market="TW")

    # Unfiltered — only user A's two rows.
    r = await client.get(
        "/api/ai/stock-reports",
        headers={"Authorization": f"Bearer {tok_a}"},
    )
    assert r.status_code == 200
    got = r.json()
    assert len(got) == 2
    assert {g["symbol"] for g in got} == {"2330", "AAPL"}
    assert all("preview" in g and g["preview"] for g in got)

    # symbol+market filter narrows to one.
    r = await client.get(
        "/api/ai/stock-reports?symbol=2330&market=TW",
        headers={"Authorization": f"Bearer {tok_a}"},
    )
    assert r.status_code == 200
    got = r.json()
    assert len(got) == 1
    assert got[0]["symbol"] == "2330"
    assert got[0]["market"] == "TW"


@pytest.mark.asyncio
async def test_get_report_scoped_to_owner(
    client: AsyncClient, db_session: AsyncSession,
):
    email_a, email_b = "sr_get_a@test.com", "sr_get_b@test.com"
    tok_a = await _register_login(client, email_a)
    tok_b = await _register_login(client, email_b)
    uid_a = await _user_id(db_session, email_a)
    row = await _seed_report(db_session, user_id=uid_a)

    # Owner sees full content + parsed sections.
    r = await client.get(
        f"/api/ai/stock-reports/{row.id}",
        headers={"Authorization": f"Bearer {tok_a}"},
    )
    assert r.status_code == 200
    detail = r.json()
    assert detail["content_md"] == _REPORT_MD
    assert detail["sections"]["結論"].startswith("偏多觀察")

    # Another user gets 404 (indistinguishable from nonexistent).
    r = await client.get(
        f"/api/ai/stock-reports/{row.id}",
        headers={"Authorization": f"Bearer {tok_b}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_requires_auth(client: AsyncClient):
    r = await client.get("/api/ai/stock-reports")
    assert r.status_code == 401


# ── section parser unit coverage ───────────────────────────────────

def test_parse_report_sections_happy_path():
    sections = parse_report_sections(_REPORT_MD)
    assert sections is not None
    assert sections["摘要"] == "台積電基本面穩健。"
    assert "非投資建議" in sections["結論"]


def test_parse_report_sections_no_headings_returns_none():
    assert parse_report_sections("純文字,沒有標題。") is None
