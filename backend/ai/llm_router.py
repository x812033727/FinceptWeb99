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
from typing import Any, AsyncGenerator, TYPE_CHECKING

import httpx

from config import settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def _resolve_api_key(provider: str, db: "AsyncSession | None") -> str:
    """Resolve the active API key for a provider.

    Priority: DB-stored (admin-set, encrypted) → .env-supplied (settings) → "".
    Empty string means "no key configured" — callers should yield that error.
    """
    if db is not None:
        from services.llm_key_service import resolve_key as _db_resolve
        try:
            key = await _db_resolve(db, provider)
            if key:
                return key
        except Exception as exc:  # noqa: BLE001 — DB hiccup must not break chat
            logger.warning("llm_key.db_lookup_failed",
                           extra={"provider": provider, "error": str(exc)})
    # settings fallback
    attr_map = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "minimax": "MINIMAX_API_KEY",
        "groq": "GROQ_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }
    attr = attr_map.get(provider, "")
    return getattr(settings, attr, "") if attr else ""


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
    openai_tool_schemas: list[dict] | None = None,
    openai_tool_dispatch: dict[str, Any] | None = None,
    db: "AsyncSession | None" = None,
) -> AsyncGenerator[dict, None]:
    """
    Yield streaming events from the chosen provider.
    `mcp_server`/`allowed_tools`/`max_turns` are honored by `claude_agent`.
    `openai_tool_schemas`/`openai_tool_dispatch` are honored by `minimax`
    (and any future OpenAI-compat tool-using provider). Other providers
    ignore both sets.

    `db` is an open AsyncSession used to resolve admin-stored API keys for
    the chosen provider. When None or no DB key is set the streamer falls
    back to the .env-supplied key in `settings`.
    """
    prov = (provider or settings.DEFAULT_LLM_PROVIDER).lower()
    api_key = await _resolve_api_key(prov, db)

    if prov == "openai":
        async for text in _openai_stream(messages, model or "gpt-4o-mini", max_tokens, temperature, api_key=api_key):
            yield {"type": "delta", "text": text}
    elif prov == "anthropic":
        async for text in _anthropic_stream(messages, model or "claude-haiku-4-5-20251001", max_tokens, temperature, api_key=api_key):
            yield {"type": "delta", "text": text}
    elif prov == "gemini":
        async for text in _gemini_stream(messages, model or "gemini-2.0-flash", max_tokens, temperature, api_key=api_key):
            yield {"type": "delta", "text": text}
    elif prov == "ollama":
        async for text in _ollama_stream(messages, model or "llama3.2", max_tokens, temperature):
            yield {"type": "delta", "text": text}
    elif prov == "minimax":
        async for ev in _minimax_stream(
            messages,
            model or settings.MINIMAX_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            tool_schemas=openai_tool_schemas,
            tool_dispatch=openai_tool_dispatch,
            max_turns=max_turns or settings.MINIMAX_MAX_TURNS,
            api_key=api_key,
        ):
            yield ev
    elif prov == "groq":
        async for ev in _groq_stream(
            messages,
            model or settings.GROQ_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            tool_schemas=openai_tool_schemas,
            tool_dispatch=openai_tool_dispatch,
            max_turns=max_turns or settings.GROQ_MAX_TURNS,
            api_key=api_key,
        ):
            yield ev
    elif prov == "deepseek":
        async for ev in _deepseek_stream(
            messages,
            model or settings.DEEPSEEK_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            tool_schemas=openai_tool_schemas,
            tool_dispatch=openai_tool_dispatch,
            max_turns=max_turns or settings.DEEPSEEK_MAX_TURNS,
            api_key=api_key,
        ):
            yield ev
    elif prov == "openrouter":
        async for ev in _openrouter_stream(
            messages,
            model or settings.OPENROUTER_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            tool_schemas=openai_tool_schemas,
            tool_dispatch=openai_tool_dispatch,
            max_turns=max_turns or settings.OPENROUTER_MAX_TURNS,
            api_key=api_key,
        ):
            yield ev
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
    *,
    api_key: str = "",
) -> AsyncGenerator[str, None]:
    try:
        from openai import AsyncOpenAI
    except ImportError:
        yield "[OpenAI SDK not installed]"
        return

    key = api_key or settings.OPENAI_API_KEY
    if not key:
        yield "[OpenAI API key not configured]"
        return

    client = AsyncOpenAI(api_key=key)
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
    *,
    api_key: str = "",
) -> AsyncGenerator[str, None]:
    try:
        import anthropic
    except ImportError:
        yield "[Anthropic SDK not installed]"
        return

    key = api_key or settings.ANTHROPIC_API_KEY
    if not key:
        yield "[Anthropic API key not configured]"
        return

    system = ""
    chat_msgs = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            chat_msgs.append(m)

    client = anthropic.AsyncAnthropic(api_key=key)
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
    *,
    api_key: str = "",
) -> AsyncGenerator[str, None]:
    try:
        import google.generativeai as genai
    except ImportError:
        yield "[google-generativeai SDK not installed]"
        return

    key = api_key or settings.GEMINI_API_KEY
    if not key:
        yield "[Gemini API key not configured]"
        return

    genai.configure(api_key=key)
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


# ── OpenAI-compatible tool loop (MiniMax / DeepSeek / Qwen API …) ──

async def _openai_compat_tool_loop(
    *,
    base_url: str,
    chat_path: str,
    api_key: str,
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    tool_schemas: list[dict] | None,
    tool_dispatch: dict[str, Any] | None,
    max_turns: int,
    provider_label: str,
    extra_headers: dict[str, str] | None = None,
) -> AsyncGenerator[dict, None]:
    """Streaming + multi-turn tool loop for OpenAI-compatible chat completions.

    Yields the unified event dict shape used by stream_chat:
        {"type": "delta", "text": "..."}
        {"type": "tool_call", "id": "...", "name": "...", "args": {...}}
        {"type": "tool_result", "id": "...", "name": "...", "summary": "...", "is_error": bool}
        {"type": "error", "message": "..."}

    Tool calls are accumulated across SSE deltas (OpenAI streams `arguments`
    as a partial JSON string), executed via `tool_dispatch`, fed back in as
    `role=tool` messages, and the loop re-calls the API until the model
    finishes with `finish_reason="stop"` or `max_turns` is reached.
    """
    if not api_key:
        yield {"type": "error", "message": f"{provider_label} API key not configured"}
        return

    url = f"{base_url.rstrip('/')}{chat_path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        **(extra_headers or {}),
    }
    convo: list[dict] = list(messages)
    tools_enabled = bool(tool_schemas and tool_dispatch)

    for turn in range(max_turns):
        payload: dict[str, Any] = {
            "model": model,
            "messages": convo,
            "stream": True,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools_enabled:
            payload["tools"] = tool_schemas
            payload["tool_choice"] = "auto"

        # accumulator: index → {id, name, arguments_str}
        pending: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        assistant_text_parts: list[str] = []
        any_text = False

        try:
            async with httpx.AsyncClient(timeout=180) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode(errors="replace")[:500]
                        yield {
                            "type": "error",
                            "message": f"{provider_label} HTTP {resp.status_code}: {body}",
                        }
                        return
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text:
                            assistant_text_parts.append(text)
                            any_text = True
                            yield {"type": "delta", "text": text}
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            slot = pending.setdefault(idx, {"id": "", "name": "", "args": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
        except Exception as exc:
            logger.exception("%s stream failed", provider_label)
            yield {"type": "error", "message": str(exc)}
            return

        if finish_reason != "tool_calls" or not pending:
            return  # natural stop, length cap, or content filter — done

        # Append the assistant's tool-call message, then execute and feed back.
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(assistant_text_parts) if any_text else None,
            "tool_calls": [
                {
                    "id": slot["id"] or f"call_{turn}_{idx}",
                    "type": "function",
                    "function": {"name": slot["name"], "arguments": slot["args"] or "{}"},
                }
                for idx, slot in sorted(pending.items())
            ],
        }
        convo.append(assistant_msg)

        for idx, slot in sorted(pending.items()):
            tool_id = slot["id"] or f"call_{turn}_{idx}"
            name = slot["name"]
            try:
                args = json.loads(slot["args"]) if slot["args"] else {}
            except json.JSONDecodeError as exc:
                err = f"invalid tool arguments JSON: {exc}"
                yield {
                    "type": "tool_result",
                    "id": tool_id, "name": name,
                    "summary": err, "is_error": True,
                }
                convo.append({
                    "role": "tool", "tool_call_id": tool_id, "name": name,
                    "content": json.dumps({"error": err}),
                })
                continue

            yield {"type": "tool_call", "id": tool_id, "name": name, "args": args}

            handler = (tool_dispatch or {}).get(name)
            if handler is None:
                err = f"unknown tool: {name}"
                result_str = json.dumps({"error": err})
                is_error = True
            else:
                try:
                    result_str = await handler(args)
                    is_error = False
                except Exception as exc:
                    logger.warning("%s tool %s failed: %s", provider_label, name, exc)
                    result_str = json.dumps({"error": str(exc)})
                    is_error = True

            yield {
                "type": "tool_result",
                "id": tool_id, "name": name,
                "summary": result_str if len(result_str) <= 2000 else result_str[:2000] + " …[truncated]",
                "is_error": is_error,
            }
            convo.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "name": name,
                "content": result_str,
            })

    yield {
        "type": "error",
        "message": f"{provider_label} hit max_turns={max_turns} without finishing",
    }


# ── MiniMax (OpenAI-compatible) ───────────────────────────────────

async def _minimax_stream(
    messages: list[dict],
    model: str,
    *,
    max_tokens: int,
    temperature: float,
    tool_schemas: list[dict] | None,
    tool_dispatch: dict[str, Any] | None,
    max_turns: int,
    api_key: str = "",
) -> AsyncGenerator[dict, None]:
    async for ev in _openai_compat_tool_loop(
        base_url=settings.MINIMAX_HOST,
        chat_path="/v1/text/chatcompletion_v2",
        api_key=api_key or settings.MINIMAX_API_KEY,
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        tool_schemas=tool_schemas,
        tool_dispatch=tool_dispatch,
        max_turns=max_turns,
        provider_label="minimax",
    ):
        yield ev


# ── Groq (OpenAI-compatible, very fast) ───────────────────────────

async def _groq_stream(
    messages: list[dict],
    model: str,
    *,
    max_tokens: int,
    temperature: float,
    tool_schemas: list[dict] | None,
    tool_dispatch: dict[str, Any] | None,
    max_turns: int,
    api_key: str = "",
) -> AsyncGenerator[dict, None]:
    async for ev in _openai_compat_tool_loop(
        base_url=settings.GROQ_HOST,
        chat_path="/v1/chat/completions",
        api_key=api_key or settings.GROQ_API_KEY,
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        tool_schemas=tool_schemas,
        tool_dispatch=tool_dispatch,
        max_turns=max_turns,
        provider_label="groq",
    ):
        yield ev


# ── DeepSeek (OpenAI-compatible, cheap, strong reasoning) ─────────

async def _deepseek_stream(
    messages: list[dict],
    model: str,
    *,
    max_tokens: int,
    temperature: float,
    tool_schemas: list[dict] | None,
    tool_dispatch: dict[str, Any] | None,
    max_turns: int,
    api_key: str = "",
) -> AsyncGenerator[dict, None]:
    async for ev in _openai_compat_tool_loop(
        base_url=settings.DEEPSEEK_HOST,
        chat_path="/v1/chat/completions",
        api_key=api_key or settings.DEEPSEEK_API_KEY,
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        tool_schemas=tool_schemas,
        tool_dispatch=tool_dispatch,
        max_turns=max_turns,
        provider_label="deepseek",
    ):
        yield ev


# ── OpenRouter (OpenAI-compatible meta-router; one key → 100+ models) ─

async def _openrouter_stream(
    messages: list[dict],
    model: str,
    *,
    max_tokens: int,
    temperature: float,
    tool_schemas: list[dict] | None,
    tool_dispatch: dict[str, Any] | None,
    max_turns: int,
    api_key: str = "",
) -> AsyncGenerator[dict, None]:
    # OpenRouter uses Referer + Title headers (optional) for app analytics.
    extra: dict[str, str] = {}
    if settings.OPENROUTER_REFERER:
        extra["HTTP-Referer"] = settings.OPENROUTER_REFERER
    if settings.OPENROUTER_TITLE:
        extra["X-Title"] = settings.OPENROUTER_TITLE
    async for ev in _openai_compat_tool_loop(
        base_url=settings.OPENROUTER_HOST,
        chat_path="/v1/chat/completions",
        api_key=api_key or settings.OPENROUTER_API_KEY,
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        tool_schemas=tool_schemas,
        tool_dispatch=tool_dispatch,
        max_turns=max_turns,
        provider_label="openrouter",
        extra_headers=extra or None,
    ):
        yield ev


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
