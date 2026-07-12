"""
B5 AI 投組健檢 API.

POST /api/ai/portfolio-review/{portfolio_id}  – SSE streaming review (auth)

On-demand analysis: the review streams to the caller and is NOT
persisted (no table, no migration). Data sources are reused wholesale —
`get_portfolio_risk` (the C1 /risk endpoint's service function) for
every number, `classify_regimes` for the current market regime; see
`services/portfolio_review_service.py`.

Ownership: a cheap owner-scoped lookup 404s BEFORE the quota is charged
or the stream starts — a foreign/unknown portfolio_id is
indistinguishable from nonexistent, and the LLM can never receive
another user's holdings because context assembly is a direct service
call bound to the authenticated user_id (防越權).

Quota / provider-key handling is identical to the B1 stock report:
one review costs one daily AI request (`_check_quota`), a stream that
yields nothing usable refunds it (`_refund_quota`), and key resolution
flows through `stream_chat(db=…, user_id=…)` (per-user DB keys →
system DB keys → .env fallback).

SSE event shapes (same contract as /stock-report — the frontend
reader is shared logic):
  data: {"stage": "context" | "generating"}   progress milestone
  data: {"delta": "..."}                       review token
  data: {"error": "..."}                       fatal error
  data: {"done": {"generated_at": "..."}}      stream finished cleanly
  data: [DONE]                                 terminator
"""
import logging
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ai.llm_router import default_model_for, stream_chat
from api.ai_agents.router import _check_quota, _refund_quota
from api.ai_agents.stock_report import _MISSING_KEY_RE, _sse
from auth.permissions import require_viewer
from config import settings
from db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter()
CurrentUser = Annotated[dict, Depends(require_viewer)]

# Four zh-TW sections plus markdown scaffolding — same headroom
# rationale as the stock report (chat's 1024 default is far too low).
_REVIEW_MAX_TOKENS = 4096
_REVIEW_TEMPERATURE = 0.4


class PortfolioReviewGenerateRequest(BaseModel):
    provider: str | None = None   # override default provider
    model: str | None = None      # override provider's default model


@router.post("/portfolio-review/{portfolio_id}")
async def generate_portfolio_review(
    portfolio_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: PortfolioReviewGenerateRequest | None = None,
):
    """Stream a structured zh-TW health-check review of one portfolio.

    Context = the C1 risk payload + current market regime + holding
    weights (all inside the risk payload); a single LLM call streams
    the review as SSE deltas. Nothing is persisted.
    """
    import services.portfolio_service as portfolio_svc

    # Owner-scoped lookup BEFORE quota/stream: missing and foreign
    # portfolios both 404 (indistinguishable), and no quota is burned
    # on a request that can't be served. `get_portfolio` raises
    # ValueError for a malformed UUID — same 404.
    try:
        portfolio = await portfolio_svc.get_portfolio(portfolio_id, user["id"], db)
    except ValueError:
        portfolio = None
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    portfolio_name = portfolio.name

    await _check_quota(user, db)

    req = body or PortfolioReviewGenerateRequest()
    provider = (req.provider or settings.DEFAULT_LLM_PROVIDER).lower()
    model = req.model or default_model_for(provider)

    async def event_generator() -> AsyncGenerator[bytes, None]:
        from services.llm_usage_service import record_usage
        from services.portfolio_review_service import (
            assemble_review_context,
            build_review_messages,
        )

        produced_content = False
        usage_seen: dict[str, int] | None = None
        parts: list[str] = []
        try:
            # Risk computation fans out 1y of history per holding +
            # Monte Carlo — signal the wait like the stock report does.
            yield _sse({"stage": "context"})
            ctx = await assemble_review_context(
                db, portfolio_id=portfolio_id, user_id=user["id"],
            )
            if (ctx.get("risk") or {}).get("empty"):
                # Nothing to review — surface a recognisable error and
                # let the finally-block refund the quota.
                yield _sse({"error": "Portfolio has no holdings to review"})
                return
            messages = build_review_messages(
                ctx, portfolio_name=portfolio_name,
            )
            yield _sse({"stage": "generating"})

            async for event in stream_chat(
                messages=messages,
                provider=provider,
                model=model,
                max_tokens=_REVIEW_MAX_TOKENS,
                temperature=_REVIEW_TEMPERATURE,
                db=db,
                user_id=user["id"],
            ):
                etype = event.get("type")
                if etype == "usage":
                    usage_seen = {
                        "prompt_tokens": int(event.get("prompt_tokens", 0)),
                        "completion_tokens": int(event.get("completion_tokens", 0)),
                    }
                    continue
                if etype == "delta":
                    text = event.get("text", "")
                    parts.append(text)
                    produced_content = True
                    yield _sse({"delta": text})
                elif etype == "error":
                    yield _sse({"error": event.get("message", "unknown error")})

            content = "".join(parts).strip()
            if produced_content and _MISSING_KEY_RE.search(content) and len(content) < 200:
                # Provider streamed only its "key not configured"
                # placeholder — treat as failure (quota refunded below).
                produced_content = False
                yield _sse({"error": content.strip("[]").strip()})
            elif produced_content and content:
                yield _sse({"done": {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }})
        except Exception as exc:  # noqa: BLE001 — must surface as SSE, not a broken pipe
            logger.error(
                "portfolio_review.stream_failed",
                extra={"portfolio_id": portfolio_id, "error": str(exc)},
                exc_info=True,
            )
            yield _sse({"error": str(exc)})
        finally:
            if not produced_content:
                await _refund_quota(user)
            if usage_seen is not None and produced_content:
                await record_usage(
                    db,
                    user_id=user["id"],
                    provider=provider,
                    model=model,
                    persona_id="portfolio_review",
                    prompt_tokens=usage_seen["prompt_tokens"],
                    completion_tokens=usage_seen["completion_tokens"],
                )
            yield b"data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
