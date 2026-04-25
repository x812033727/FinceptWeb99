"""
AI Agents API.

GET  /api/ai/agents               – list available agents
POST /api/ai/chat                 – SSE streaming chat (requires auth)

Quota: analyst → 20 req/day, viewer → 5 req/day (Redis-backed, key_ai_counter).

SSE event shapes:
  data: {"delta": "..."}                                               (all providers)
  data: {"tool_call": {"id": "...", "name": "...", "args": {...}}}     (claude_agent only)
  data: {"tool_result": {"id": "...", "name": "...", "summary": "...", "is_error": false}}
  data: {"error": "..."}                                               (any fatal error)
  data: [DONE]                                                         (terminator)

The `claude_agent` provider is gated by feature flag + analyst/admin role
because it reads user-owned data and can execute tools.
"""
import json
import logging
from typing import Annotated, AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from api.ai_agents.schemas import AgentInfo, ChatRequest
from auth.permissions import require_viewer
from cache.redis_cache import cache_decr, cache_incr, key_ai_counter
from ai.agents import get_agent, list_agents
from ai.llm_router import stream_chat
from config import settings

logger = logging.getLogger(__name__)
router = APIRouter()
CurrentUser = Annotated[dict, Depends(require_viewer)]


# ── quota helpers ─────────────────────────────────────────────────

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


async def _refund_quota(user: dict) -> None:
    """Decrement the daily AI quota counter so a user isn't charged for a
    request that produced no usable output (validation failure before the
    LLM call, or a stream that yielded zero tokens).

    Failure here means the user's quota stays incremented even though they
    got nothing back. We log at error level so this surfaces in alerts and
    operators can manually decrement if needed.
    """
    try:
        await cache_decr(key_ai_counter(user["id"]))
    except Exception as exc:
        logger.error(
            "ai.quota.refund_failed",
            extra={"user_id": user.get("id"), "error": str(exc)},
            exc_info=True,
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

    Attach structured context (e.g. DCF result, portfolio snapshot) in
    the `context` field — it will be prepended to the system prompt as
    a JSON block. Pass `provider="claude_agent"` as override (or pick an
    agent whose default_provider is claude_agent) to enable tool-use.
    """
    await _check_quota(user)

    try:
        agent = get_agent(body.agent_id)
    except ValueError as e:
        await _refund_quota(user)
        raise HTTPException(status_code=400, detail=str(e))

    system_content = agent.system_prompt
    if body.context:
        ctx_json = json.dumps(body.context, indent=2, ensure_ascii=False)
        system_content += f"\n\n<context>\n{ctx_json}\n</context>"

    messages: list[dict] = [{"role": "system", "content": system_content}]
    for m in body.messages:
        messages.append({"role": m.role, "content": m.content})

    provider = (body.provider or agent.default_provider).lower()
    model = body.model or agent.default_model

    mcp_server = None
    allowed_tools: list[str] = []
    if provider == "claude_agent":
        if not settings.CLAUDE_AGENT_ENABLED:
            await _refund_quota(user)
            raise HTTPException(status_code=503, detail="Claude Agent is disabled")
        if user.get("role") not in ("analyst", "admin"):
            await _refund_quota(user)
            raise HTTPException(status_code=403, detail="Claude Agent requires analyst role")
        # Lazy import — keeps SDK out of the hot path for other providers
        from ai.tools import build_toolset, tool_names
        mcp_server = build_toolset(user["id"])
        allowed_tools = tool_names()

    async def event_generator() -> AsyncGenerator[bytes, None]:
        # Refund the quota when the stream produces no usable content —
        # i.e. the provider raised before any delta or yielded only errors.
        produced_content = False
        try:
            async for event in stream_chat(
                messages=messages,
                provider=provider,
                model=model,
                mcp_server=mcp_server,
                allowed_tools=allowed_tools,
                max_turns=settings.CLAUDE_AGENT_MAX_TURNS,
            ):
                payload = _event_to_sse(event)
                if payload is not None:
                    yield payload
                    if event.get("type") in ("delta", "tool_call", "tool_result"):
                        produced_content = True
        except Exception as exc:
            err = json.dumps({"error": str(exc)}, ensure_ascii=False)
            yield f"data: {err}\n\n".encode()
        finally:
            if not produced_content:
                await _refund_quota(user)
            yield b"data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _event_to_sse(event: dict) -> bytes | None:
    """Translate internal event dict into an SSE `data:` frame.

    Unknown event types are dropped (forward-compat: we can add new types
    in the router without breaking old clients).
    """
    etype = event.get("type")
    if etype == "delta":
        out = {"delta": event.get("text", "")}
    elif etype == "tool_call":
        out = {"tool_call": {
            "id": event.get("id"),
            "name": event.get("name"),
            "args": event.get("args"),
        }}
    elif etype == "tool_result":
        out = {"tool_result": {
            "id": event.get("id"),
            "name": event.get("name"),
            "summary": event.get("summary", ""),
            "is_error": event.get("is_error", False),
        }}
    elif etype == "error":
        out = {"error": event.get("message", "unknown error")}
    else:
        return None
    return f"data: {json.dumps(out, ensure_ascii=False)}\n\n".encode()
