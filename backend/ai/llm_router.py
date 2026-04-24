"""
LLM Router — streams events from OpenAI / Anthropic / Gemini / Ollama / Claude Agent.

`stream_chat` yields dicts so the endpoint can transport more than plain text:
  {"type": "delta", "text": "..."}                 text token (all providers)
  {"type": "tool_call", "id": "...", "name": "...", "args": {...}}     claude_agent only
  {"type": "tool_result", "id": "...", "name": "...", "summary": "..."}  claude_agent only
  {"type": "error", "message": "..."}              provider-level failure

The four non-Claude providers emit only `delta`. The chat endpoint translates
every event into its SSE wire form (see api/ai_agents/router.py).
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator

import httpx

from config import settings

logger = logging.getLogger(__name__)


# ── provider dispatch ──────────────────────────────────────────────

async def stream_chat(
    messages: list[dict],
    provider: str | None = None,
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.3,
    mcp_server: Any = None,
    allowed_tools: list[str] | None = None,
    max_turns: int | None = None,
) -> AsyncGenerator[dict, None]:
    """
    Yield streaming events from the chosen provider.
    `mcp_server`/`allowed_tools`/`max_turns` are only honored by the
    `claude_agent` provider; other providers ignore them.
    """
    prov = (provider or settings.DEFAULT_LLM_PROVIDER).lower()

    if prov == "openai":
        async for text in _openai_stream(messages, model or "gpt-4o-mini", max_tokens, temperature):
            yield {"type": "delta", "text": text}
    elif prov == "anthropic":
        async for text in _anthropic_stream(messages, model or "claude-haiku-4-5-20251001", max_tokens, temperature):
            yield {"type": "delta", "text": text}
    elif prov == "gemini":
        async for text in _gemini_stream(messages, model or "gemini-2.0-flash", max_tokens, temperature):
            yield {"type": "delta", "text": text}
    elif prov == "ollama":
        async for text in _ollama_stream(messages, model or "llama3.2", max_tokens, temperature):
            yield {"type": "delta", "text": text}
    elif prov == "claude_agent":
        async for ev in _claude_agent_stream(
            messages,
            model or settings.CLAUDE_AGENT_MODEL,
            mcp_server=mcp_server,
            allowed_tools=allowed_tools or [],
            max_turns=max_turns or settings.CLAUDE_AGENT_MAX_TURNS,
        ):
            yield ev
    else:
        raise ValueError(f"Unknown LLM provider: {prov}")


# ── OpenAI ────────────────────────────────────────────────────────

async def _openai_stream(
    messages: list[dict],
    model: str,
    max_tokens: int,
    temperature: float,
) -> AsyncGenerator[str, None]:
    try:
        from openai import AsyncOpenAI
    except ImportError:
        yield "[OpenAI SDK not installed]"
        return

    if not settings.OPENAI_API_KEY:
        yield "[OpenAI API key not configured]"
        return

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    async with client.chat.completions.stream(
        model=model,
        messages=messages,  # type: ignore[arg-type]
        max_tokens=max_tokens,
        temperature=temperature,
    ) as stream:
        async for event in stream:
            delta = event.choices[0].delta.content if event.choices else None
            if delta:
                yield delta


# ── Anthropic ─────────────────────────────────────────────────────

async def _anthropic_stream(
    messages: list[dict],
    model: str,
    max_tokens: int,
    temperature: float,
) -> AsyncGenerator[str, None]:
    try:
        import anthropic
    except ImportError:
        yield "[Anthropic SDK not installed]"
        return

    if not settings.ANTHROPIC_API_KEY:
        yield "[Anthropic API key not configured]"
        return

    system = ""
    chat_msgs = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            chat_msgs.append(m)

    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    async with client.messages.stream(
        model=model,
        system=system or anthropic.NOT_GIVEN,
        messages=chat_msgs,  # type: ignore[arg-type]
        max_tokens=max_tokens,
        temperature=temperature,
    ) as stream:
        async for text in stream.text_stream:
            yield text


# ── Gemini ────────────────────────────────────────────────────────

async def _gemini_stream(
    messages: list[dict],
    model: str,
    max_tokens: int,
    temperature: float,
) -> AsyncGenerator[str, None]:
    try:
        import google.generativeai as genai
    except ImportError:
        yield "[google-generativeai SDK not installed]"
        return

    if not settings.GEMINI_API_KEY:
        yield "[Gemini API key not configured]"
        return

    genai.configure(api_key=settings.GEMINI_API_KEY)
    gmodel = genai.GenerativeModel(model)

    history = []
    prompt = ""
    for m in messages:
        if m["role"] == "system":
            prompt += m["content"] + "\n\n"
        elif m["role"] == "user":
            history.append({"role": "user", "parts": [m["content"]]})
        elif m["role"] == "assistant":
            history.append({"role": "model", "parts": [m["content"]]})

    if history and history[-1]["role"] == "user":
        last_user = history.pop()
        prompt += last_user["parts"][0]

    chat = gmodel.start_chat(history=history)
    response = await chat.send_message_async(
        prompt,
        stream=True,
        generation_config=genai.types.GenerationConfig(
            max_output_tokens=max_tokens,
            temperature=temperature,
        ),
    )
    async for chunk in response:
        if chunk.text:
            yield chunk.text


# ── Ollama ────────────────────────────────────────────────────────

async def _ollama_stream(
    messages: list[dict],
    model: str,
    max_tokens: int,
    temperature: float,
) -> AsyncGenerator[str, None]:
    url = f"{settings.OLLAMA_HOST.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"num_predict": max_tokens, "temperature": temperature},
    }
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", url, json=payload) as response:
            if response.status_code != 200:
                yield f"[Ollama error: {response.status_code}]"
                return
            async for line in response.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    content = data.get("message", {}).get("content", "")
                    if content:
                        yield content
                    if data.get("done"):
                        break
                except json.JSONDecodeError:
                    continue


# ── Claude Agent (tool-use via claude-agent-sdk) ───────────────────

def _flatten_prompt(messages: list[dict]) -> tuple[str, str]:
    """Split messages into (system_prompt, user_prompt). Claude Agent takes
    a single system string + a single user turn per query()."""
    system_parts: list[str] = []
    user_parts: list[str] = []
    for m in messages:
        if m["role"] == "system":
            system_parts.append(str(m["content"]))
        elif m["role"] == "user":
            user_parts.append(str(m["content"]))
        elif m["role"] == "assistant":
            # Fold assistant turns into the prompt as context — the SDK
            # session doesn't accept pre-written assistant turns directly.
            user_parts.append(f"<previous_assistant>{m['content']}</previous_assistant>")
    return "\n\n".join(system_parts), "\n\n".join(user_parts)


def _truncate(value: Any, limit: int = 2000) -> str:
    s = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    return s if len(s) <= limit else s[:limit] + " …[truncated]"


async def _claude_agent_stream(
    messages: list[dict],
    model: str,
    mcp_server: Any,
    allowed_tools: list[str],
    max_turns: int,
) -> AsyncGenerator[dict, None]:
    """Stream from claude-agent-sdk, translating SDK message blocks into
    our unified event dict shape."""
    if not settings.ANTHROPIC_API_KEY:
        yield {"type": "error", "message": "ANTHROPIC_API_KEY not configured"}
        return
    if mcp_server is None:
        yield {"type": "error", "message": "No MCP server bound; toolset missing"}
        return

    try:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
    except ImportError as exc:
        yield {"type": "error", "message": f"claude-agent-sdk not installed: {exc}"}
        return

    system, user_prompt = _flatten_prompt(messages)
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system or None,
        mcp_servers={"fincept": mcp_server},
        allowed_tools=allowed_tools,
        max_turns=max_turns,
        permission_mode="bypassPermissions",  # tools are already gated upstream
    )

    # Track tool_call → tool_result pairing via tool_use_id
    pending: dict[str, str] = {}  # id -> name

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(user_prompt)
            async for msg in client.receive_response():
                # AssistantMessage carries text + tool_use blocks
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            if block.text:
                                yield {"type": "delta", "text": block.text}
                        elif isinstance(block, ToolUseBlock):
                            pending[block.id] = block.name
                            yield {
                                "type": "tool_call",
                                "id": block.id,
                                "name": block.name,
                                "args": block.input,
                            }
                # UserMessage can carry tool_result blocks the SDK synthesised
                elif msg.__class__.__name__ == "UserMessage":
                    content = getattr(msg, "content", None)
                    if isinstance(content, list):
                        for block in content:
                            # ToolResultBlock shape: tool_use_id + content
                            tool_use_id = getattr(block, "tool_use_id", None)
                            if tool_use_id:
                                raw = getattr(block, "content", "")
                                if isinstance(raw, list):
                                    # MCP returns [{"type":"text","text":"..."}]
                                    raw = "".join(
                                        b.get("text", "") if isinstance(b, dict) else str(b)
                                        for b in raw
                                    )
                                yield {
                                    "type": "tool_result",
                                    "id": tool_use_id,
                                    "name": pending.get(tool_use_id, ""),
                                    "summary": _truncate(raw),
                                    "is_error": bool(getattr(block, "is_error", False)),
                                }
                elif isinstance(msg, ResultMessage):
                    if msg.is_error:
                        yield {"type": "error", "message": msg.result or "claude_agent returned error"}
                    break  # Terminal
    except Exception as exc:
        logger.exception("claude_agent stream failed")
        yield {"type": "error", "message": str(exc)}
