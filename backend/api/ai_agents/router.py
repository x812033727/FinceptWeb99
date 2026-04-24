"""
AI Agents API.

GET  /api/ai/agents               – list available agents
POST /api/ai/chat                 – SSE streaming chat (requires auth)

Quota enforcement via Redis: analyst → 20 req/day, viewer → 5 req/day.
Each SSE event is:  data: <json>\n\n
Final event:        data: [DONE]\n\n
Error event:        data: {"error": "..."}\n\n
"""
import json
from typing import Annotated, AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from api.ai_agents.schemas import ChatRequest, AgentInfo
from auth.permissions import require_viewer
from cache.redis_cache import cache_incr, key_ai_counter
from ai.agents import get_agent, list_agents
from ai.llm_router import stream_chat
from config import settings

router = APIRouter()
CurrentUser = Annotated[dict, Depends(require_viewer)]


# ── quota helper ──────────────────────────────────────────────────

async def _check_quota(user: dict) -> None:
    role = user.get("role", "viewer")
    limit = (
        settings.AI_REQUESTS_ANALYST_DAILY
        if role in ("analyst", "admin")
        else settings.AI_REQUESTS_VIEWER_DAILY
    )
    count = await cache_incr(key_ai_counter(user["id"]), ttl_seconds=86400)
    if count > limit:
        raise HTTPException(
            status_code=429,
            detail=f"Daily AI quota exceeded ({limit} requests/day). Resets at midnight UTC.",
        )


# ── routes ────────────────────────────────────────────────────────

@router.get("/agents", response_model=list[AgentInfo])
async def agents_list(_: CurrentUser):
    """Return the list of available AI agent personas."""
    return list_agents()


@router.post("/chat")
async def chat(body: ChatRequest, user: CurrentUser):
    """
    Stream a chat response from the selected agent persona.
    Returns Server-Sent Events (text/event-stream).

    Attach structured context (e.g. DCF result, portfolio snapshot) in
    the `context` field and it will be prepended to the system prompt as
    a JSON block.
    """
    await _check_quota(user)

    try:
        agent = get_agent(body.agent_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Build system prompt, optionally with injected context
    system_content = agent.system_prompt
    if body.context:
        ctx_json = json.dumps(body.context, indent=2, ensure_ascii=False)
        system_content += f"\n\n<context>\n{ctx_json}\n</context>"

    messages: list[dict] = [{"role": "system", "content": system_content}]
    for m in body.messages:
        messages.append({"role": m.role, "content": m.content})

    provider = body.provider or agent.default_provider
    model = body.model or agent.default_model

    async def event_generator() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in stream_chat(
                messages=messages,
                provider=provider,
                model=model,
            ):
                payload = json.dumps({"delta": chunk}, ensure_ascii=False)
                yield f"data: {payload}\n\n".encode()
        except Exception as exc:
            err = json.dumps({"error": str(exc)}, ensure_ascii=False)
            yield f"data: {err}\n\n".encode()
        finally:
            yield b"data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
        },
    )
