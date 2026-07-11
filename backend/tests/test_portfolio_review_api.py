"""
B5 AI 投組健檢 API tests — POST /api/ai/portfolio-review/{portfolio_id}.

Covers: auth gating, ownership scoping (404 for nonexistent AND
foreign portfolios — the 防越權 guarantee that the LLM can never be
pointed at another user's holdings), daily-quota enforcement shared
with /chat and /stock-report, the SSE stream flow with the risk
service called exactly once (LLM + risk mocked), the empty-portfolio
short-circuit, quota refund on stream failure, and the
missing-provider-key guard. Nothing is persisted (on-demand analysis
— asserted by streaming to completion without any table involved).
"""
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

_REVIEW_MD = (
    "## 總評\n投組體質穩健,規模適中。\n\n"
    "## 集中度與風險\nAAPL 權重 60% 超過 25% 門檻;95% VaR 約 2.1%。\n\n"
    "## 與當前市場情勢的適配\n目前台股為多頭情勢,高 beta 配置尚屬合理。\n\n"
    "## 行動建議\n- 降低 AAPL 權重至 40% 以下 — 單一持倉集中度警示(優先:高)\n"
    "- 增加低相關性資產 — 持倉相關性 0.8 偏高(優先:中)\n\n"
    "**本報告由 AI 產生,僅供研究參考,非投資建議。**"
)

_FAKE_RISK = {
    "portfolio_id": "pid",
    "currency": "USD",
    "as_of": "2026-07-12",
    "portfolio_value": 10_000.0,
    "observations": 250,
    "empty": False,
    "benchmark": "SPY",
    "metrics": {
        "annualised_return": 0.12, "annualised_volatility": 0.18,
        "sharpe_ratio": 0.9, "sortino_ratio": 1.1, "calmar_ratio": 0.8,
        "max_drawdown": -0.15, "beta": 1.05,
    },
    "var": [{"method": "historical", "confidence_level": 0.95,
             "var_pct": 0.021, "var_amount": 210.0}],
    "weights": [
        {"symbol": "AAPL", "market": "US", "weight_pct": 60.0,
         "risk_contribution_pct": 70.0},
        {"symbol": "MSFT", "market": "US", "weight_pct": 40.0,
         "risk_contribution_pct": 30.0},
    ],
    "correlation": {"symbols": ["AAPL", "MSFT"],
                    "matrix": [[1.0, 0.8], [0.8, 1.0]]},
    "warnings": [{"kind": "single_position", "key": "AAPL",
                  "weight_pct": 60.0, "threshold_pct": 25.0}],
    "excluded": [],
}

_EMPTY_RISK = {
    "portfolio_id": "pid", "currency": "USD", "as_of": "2026-07-12",
    "portfolio_value": 0.0, "observations": 0, "empty": True,
    "benchmark": None, "metrics": None, "var": [], "weights": [],
    "correlation": None, "warnings": [], "excluded": [],
}


# ── helpers ────────────────────────────────────────────────────────

async def _register_login(client: AsyncClient, email: str) -> str:
    await client.post("/api/auth/register", json={"email": email, "password": "Pass99!!"})
    r = await client.post("/api/auth/login", json={"email": email, "password": "Pass99!!"})
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_portfolio(client: AsyncClient, token: str, name: str = "ReviewFund") -> str:
    r = await client.post(
        "/api/portfolio",
        json={"name": name, "currency": "USD"},
        headers=_auth(token),
    )
    return r.json()["id"]


def _delta_stream(chunks: list[str], with_usage: bool = True):
    """Async-generator factory that mimics `stream_chat` output."""
    async def gen(*_a, **_kw):
        for c in chunks:
            yield {"type": "delta", "text": c}
        if with_usage:
            yield {"type": "usage", "prompt_tokens": 100, "completion_tokens": 50}
    return gen


def _risk_patch(payload: dict = _FAKE_RISK):
    """Patch the C1 risk service at its home module — the review
    service late-imports it, so this is the single seam."""
    return patch(
        "services.portfolio_risk_service.get_portfolio_risk",
        new=AsyncMock(return_value=payload),
    )


def _regime_patch(bands: list[dict] | None = None):
    return patch(
        "services.regime_classifier.classify_regimes",
        new=AsyncMock(return_value=bands or []),
    )


# ── auth / ownership scoping (防越權) ──────────────────────────────

@pytest.mark.asyncio
async def test_review_requires_auth(client: AsyncClient):
    r = await client.post(
        "/api/ai/portfolio-review/00000000-0000-0000-0000-000000000000",
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_review_nonexistent_portfolio_returns_404(client: AsyncClient):
    tok = await _register_login(client, "pr_404@test.com")
    r = await client.post(
        "/api/ai/portfolio-review/00000000-0000-0000-0000-000000000000",
        headers=_auth(tok),
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_review_malformed_id_returns_404(client: AsyncClient):
    tok = await _register_login(client, "pr_badid@test.com")
    r = await client.post(
        "/api/ai/portfolio-review/not-a-uuid",
        headers=_auth(tok),
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_review_cross_user_returns_404_and_no_context_leaks(
    client: AsyncClient,
):
    """防越權: user B requesting user A's portfolio gets a 404
    indistinguishable from nonexistent, BEFORE any context assembly —
    the LLM path never sees another user's holdings."""
    tok_a = await _register_login(client, "pr_own_a@test.com")
    tok_b = await _register_login(client, "pr_own_b@test.com")
    pid = await _create_portfolio(client, tok_a, "PrivateFund")

    with _risk_patch() as risk:
        r = await client.post(
            f"/api/ai/portfolio-review/{pid}",
            headers=_auth(tok_b),
        )
    assert r.status_code == 404
    # Context assembly (and therefore any prompt build) never ran.
    risk.assert_not_awaited()


# ── quota ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_review_enforces_daily_quota(client: AsyncClient, mock_redis):
    """Same Redis counter as /chat and /stock-report — a viewer past
    the daily limit gets 429 and the risk computation never starts."""
    tok = await _register_login(client, "pr_quota@test.com")
    pid = await _create_portfolio(client, tok)
    mock_redis.incr.return_value = 999  # way past the viewer limit
    with _risk_patch() as risk:
        r = await client.post(
            f"/api/ai/portfolio-review/{pid}",
            headers=_auth(tok),
        )
    assert r.status_code == 429
    assert "quota" in r.json()["detail"].lower()
    risk.assert_not_awaited()


# ── streaming happy path ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_stream_yields_deltas_and_calls_risk_service_once(
    client: AsyncClient,
):
    """The C1 risk service is the single data source: called exactly
    once, with the caller's own user_id, and its payload feeds the
    stream that completes with done + [DONE]."""
    tok = await _register_login(client, "pr_happy@test.com")
    pid = await _create_portfolio(client, tok)

    chunks = [_REVIEW_MD[:50], _REVIEW_MD[50:]]
    with _risk_patch() as risk, _regime_patch([
        {"start": "2026-06-01", "end": "2026-07-11", "regime": "bull"},
        {"start": "2026-07-01", "end": "2026-07-11", "regime": "low_vol"},
    ]), patch(
        "api.ai_agents.portfolio_review.stream_chat",
        side_effect=_delta_stream(chunks),
    ) as chat:
        r = await client.post(
            f"/api/ai/portfolio-review/{pid}",
            headers=_auth(tok),
        )
        body = r.text

    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    # Stage events, deltas, done marker, terminator all present.
    assert '"stage": "context"' in body
    assert '"stage": "generating"' in body
    assert "## 總評" in body
    assert '"generated_at"' in body
    assert "data: [DONE]" in body

    # Risk math reused wholesale — one call, owner-scoped args.
    risk.assert_awaited_once()
    args = risk.await_args.args
    assert args[0] == pid
    # Second positional arg is the authenticated user's id (uuid str).
    assert isinstance(args[1], str) and len(args[1]) == 36

    # The prompt fed to the LLM embeds the risk numbers + regime tags.
    chat.assert_called_once()
    messages = chat.call_args.kwargs["messages"]
    assert messages[0]["role"] == "system"
    for section in ("總評", "集中度與風險", "與當前市場情勢的適配", "行動建議"):
        assert section in messages[0]["content"]
    user_msg = messages[1]["content"]
    assert "AAPL" in user_msg
    assert "single_position" in user_msg
    assert "bull" in user_msg and "low_vol" in user_msg
    # Key/quota plumbing identical to stock-report: stream_chat gets
    # db + user_id so per-user keys resolve.
    assert chat.call_args.kwargs["user_id"]
    assert "db" in chat.call_args.kwargs


@pytest.mark.asyncio
async def test_stream_empty_portfolio_errors_and_refunds(client: AsyncClient):
    """A portfolio with no holdings can't be reviewed — SSE error,
    no LLM call, quota refunded."""
    tok = await _register_login(client, "pr_empty@test.com")
    pid = await _create_portfolio(client, tok)

    with _risk_patch(_EMPTY_RISK), _regime_patch(), \
         patch("api.ai_agents.portfolio_review.stream_chat") as chat, \
         patch("api.ai_agents.portfolio_review._refund_quota",
               new_callable=AsyncMock) as refund:
        r = await client.post(
            f"/api/ai/portfolio-review/{pid}",
            headers=_auth(tok),
        )
        body = r.text
    assert r.status_code == 200
    assert "no holdings" in body
    assert "data: [DONE]" in body
    chat.assert_not_called()
    refund.assert_awaited_once()


# ── failure paths ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stream_error_refunds_quota(client: AsyncClient):
    tok = await _register_login(client, "pr_err@test.com")
    pid = await _create_portfolio(client, tok)

    async def boom(*_a, **_kw):
        raise RuntimeError("provider exploded")
        yield  # pragma: no cover

    with _risk_patch(), _regime_patch(), \
         patch("api.ai_agents.portfolio_review.stream_chat", side_effect=boom), \
         patch("api.ai_agents.portfolio_review._refund_quota",
               new_callable=AsyncMock) as refund:
        r = await client.post(
            f"/api/ai/portfolio-review/{pid}",
            headers=_auth(tok),
        )
        body = r.text
    assert r.status_code == 200
    assert "provider exploded" in body
    assert "data: [DONE]" in body
    refund.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_api_key_placeholder_becomes_error_and_refunds(
    client: AsyncClient,
):
    """Providers with no key stream a placeholder delta instead of an
    error — the endpoint must convert it to an SSE error and refund
    (same guard as the stock report)."""
    tok = await _register_login(client, "pr_no_key@test.com")
    pid = await _create_portfolio(client, tok)

    with _risk_patch(), _regime_patch(), \
         patch("api.ai_agents.portfolio_review.stream_chat",
               side_effect=_delta_stream(["[OpenAI API key not configured]"],
                                         with_usage=False)), \
         patch("api.ai_agents.portfolio_review._refund_quota",
               new_callable=AsyncMock) as refund:
        r = await client.post(
            f"/api/ai/portfolio-review/{pid}",
            headers=_auth(tok),
        )
        body = r.text
    assert r.status_code == 200
    assert '"error"' in body
    assert "API key not configured" in body
    refund.assert_awaited_once()


# ── regime helper unit coverage ────────────────────────────────────

@pytest.mark.asyncio
async def test_get_current_regime_picks_latest_bands():
    from services.portfolio_review_service import get_current_regime

    bands = [
        {"start": "2026-05-01", "end": "2026-06-10", "regime": "bear"},
        {"start": "2026-06-11", "end": "2026-07-11", "regime": "bull"},
        {"start": "2026-07-01", "end": "2026-07-11", "regime": "low_vol"},
    ]
    with _regime_patch(bands):
        out = await get_current_regime(db=None)
    assert out["regimes"] == ["bull", "low_vol"]
    assert out["as_of"] == "2026-07-11"
    assert any("多頭" in z for z in out["regimes_zh"])


@pytest.mark.asyncio
async def test_get_current_regime_no_data_degrades():
    from services.portfolio_review_service import get_current_regime

    with _regime_patch([]):
        out = await get_current_regime(db=None)
    assert out["regimes"] == []
    assert out["as_of"] is None
