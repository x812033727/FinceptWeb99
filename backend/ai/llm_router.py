"""
LLM Router — streams events from OpenAI / Anthropic / Gemini / Ollama / Claude Agent.

`stream_chat` yields dicts so the endpoint can transport more than plain text:
  {"type": "delta", "text": "..."}                 text token (all providers)
  {"type": "tool_call", "id": "...", "name": "...", "args": {...}}     claude_agent only
  {"type": "tool_result", "id": "...", "name": "...", "summary": "..."}  claude_agent only
  {"type": "usage", "prompt_tokens": int, "completion_tokens": int}     emitted at end
  {"type": "error", "message": "..."}              provider-level failure

The chat endpoint records the `usage` event into llm_usage_events for cost
tracking. Providers that don't expose token counts simply don't emit usage.
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


# ── Provider registry ─────────────────────────────────────────────
#
# Single source of truth for everything per-provider that used to live
# in three drifting tables: `stream_chat`'s if/elif dispatch,
# `default_model_for`'s dict, and `_resolve_api_key`'s attr_map.
# Adding a provider is now ONE entry here (plus its `_*_stream`
# implementation). The R0 characterization tests
# (tests/test_llm_router_dispatch_characterization.py) freeze the
# observable behavior of all three surfaces across this structure.


class ProviderSpec:
    """Per-provider wiring for `stream_chat`.

    `stream_attr` is the NAME of the module-level `_*_stream` function,
    resolved via `globals()` at call time — late binding keeps
    `patch.object(llm_router, "_openai_stream", …)` working in tests
    and lets reloads swap implementations.

    `kind` selects the call signature:
      - "simple":        (messages, model, max_tokens, temperature, api_key=)
      - "no_key":        (messages, model, max_tokens, temperature)
      - "openai_compat": OpenAI-style tool loop (tool_schemas/dispatch/
                         max_turns from `max_turns_attr`)
      - "claude_agent":  claude-agent-sdk session (mcp_server/
                         allowed_tools/max_turns)

    `default_model` is a zero-arg callable so specs that follow a
    settings value keep reading it lazily. `env_key_attr` is the
    settings attribute `_resolve_api_key` falls back to (None for
    keyless providers — ollama, and claude_agent which reads
    ANTHROPIC_API_KEY from the environment inside the SDK).
    """

    __slots__ = ("kind", "stream_attr", "default_model", "env_key_attr", "max_turns_attr")

    def __init__(
        self,
        kind: str,
        stream_attr: str,
        default_model,
        env_key_attr: str | None = None,
        max_turns_attr: str | None = None,
    ) -> None:
        self.kind = kind
        self.stream_attr = stream_attr
        self.default_model = default_model
        self.env_key_attr = env_key_attr
        self.max_turns_attr = max_turns_attr


PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        "simple", "_openai_stream", lambda: "gpt-4o-mini", "OPENAI_API_KEY",
    ),
    "anthropic": ProviderSpec(
        "simple", "_anthropic_stream", lambda: "claude-haiku-4-5-20251001",
        "ANTHROPIC_API_KEY",
    ),
    "gemini": ProviderSpec(
        "simple", "_gemini_stream", lambda: "gemini-2.0-flash", "GEMINI_API_KEY",
    ),
    "ollama": ProviderSpec(
        "no_key", "_ollama_stream", lambda: "llama3.2",
    ),
    "minimax": ProviderSpec(
        "openai_compat", "_minimax_stream", lambda: settings.MINIMAX_MODEL,
        "MINIMAX_API_KEY", "MINIMAX_MAX_TURNS",
    ),
    "groq": ProviderSpec(
        "openai_compat", "_groq_stream", lambda: settings.GROQ_MODEL,
        "GROQ_API_KEY", "GROQ_MAX_TURNS",
    ),
    "deepseek": ProviderSpec(
        "openai_compat", "_deepseek_stream", lambda: settings.DEEPSEEK_MODEL,
        "DEEPSEEK_API_KEY", "DEEPSEEK_MAX_TURNS",
    ),
    "openrouter": ProviderSpec(
        "openai_compat", "_openrouter_stream", lambda: settings.OPENROUTER_MODEL,
        "OPENROUTER_API_KEY", "OPENROUTER_MAX_TURNS",
    ),
    "claude_agent": ProviderSpec(
        "claude_agent", "_claude_agent_stream", lambda: settings.CLAUDE_AGENT_MODEL,
    ),
}


async def _resolve_api_key(
    provider: str, db: "AsyncSession | None", user_id: "str | None" = None,
) -> str:
    """Resolve the active API key for a provider.

    Priority: per-user DB row (if user_id given) → system DB row →
    .env-supplied (settings) → "". Empty means "no key configured" —
    callers should yield that error.
    """
    if db is not None:
        from services.llm_key_service import resolve_key as _db_resolve
        import uuid as _uuid
        uid = None
        if user_id:
            try:
                uid = _uuid.UUID(user_id)
            except (TypeError, ValueError):
                uid = None
        try:
            key = await _db_resolve(db, provider, uid)
            if key:
                return key
        except Exception as exc:  # noqa: BLE001 — DB hiccup must not break chat
            logger.warning("llm_key.db_lookup_failed",
                           extra={"provider": provider, "error": str(exc)})
    # settings fallback — attr comes from the provider registry.
    spec = PROVIDERS.get(provider)
    attr = spec.env_key_attr if spec else None
    return getattr(settings, attr, "") if attr else ""


def default_model_for(provider: str | None) -> str:
    """Default model id used when a caller doesn't specify one.

    Mirrors the per-provider fallbacks in `stream_chat`'s dispatch
    below — callers that need to LABEL the model actually used (e.g.
    the stock-report archive persisting a `model` column) resolve it
    here instead of duplicating the literals.
    """
    prov = (provider or settings.DEFAULT_LLM_PROVIDER).lower()
    spec = PROVIDERS.get(prov)
    return spec.default_model() if spec else ""


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
    user_id: str | None = None,
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
    spec = PROVIDERS.get(prov)
    if spec is None:
        raise ValueError(f"Unknown LLM provider: {prov}")

    api_key = await _resolve_api_key(prov, db, user_id)
    # Late-bound lookup so tests patching `llm_router._x_stream` (and
    # any runtime monkeypatching) intercept the call.
    stream_fn = globals()[spec.stream_attr]
    resolved_model = model or spec.default_model()

    if spec.kind == "simple":
        agen = stream_fn(messages, resolved_model, max_tokens, temperature, api_key=api_key)
    elif spec.kind == "no_key":
        agen = stream_fn(messages, resolved_model, max_tokens, temperature)
    elif spec.kind == "openai_compat":
        agen = stream_fn(
            messages,
            resolved_model,
            max_tokens=max_tokens,
            temperature=temperature,
            tool_schemas=openai_tool_schemas,
            tool_dispatch=openai_tool_dispatch,
            max_turns=max_turns or getattr(settings, spec.max_turns_attr),
            api_key=api_key,
        )
    elif spec.kind == "claude_agent":
        agen = stream_fn(
            messages,
            resolved_model,
            mcp_server=mcp_server,
            allowed_tools=allowed_tools or [],
            max_turns=max_turns or settings.CLAUDE_AGENT_MAX_TURNS,
        )
    else:  # pragma: no cover — registry entries own their kind
        raise ValueError(f"Unknown provider kind: {spec.kind}")

    async for ev in agen:
        yield ev


# ── OpenAI ────────────────────────────────────────────────────────

async def _openai_stream(
    messages: list[dict],
    model: str,
    max_tokens: int,
    temperature: float,
    *,
    api_key: str = "",
) -> AsyncGenerator[dict, None]:
    try:
        from openai import AsyncOpenAI
    except ImportError:
        yield {"type": "delta", "text": "[OpenAI SDK not installed]"}
        return

    key = api_key or settings.OPENAI_API_KEY
    if not key:
        yield {"type": "delta", "text": "[OpenAI API key not configured]"}
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
                yield {"type": "delta", "text": delta}
        # Final aggregated chunk carries usage.
        try:
            final = await stream.get_final_completion()
            usage = getattr(final, "usage", None)
            if usage:
                yield {
                    "type": "usage",
                    "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                    "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                }
        except Exception as exc:  # noqa: BLE001
            logger.debug("openai.usage_unavailable: %s", exc)


# ── Anthropic ─────────────────────────────────────────────────────

async def _anthropic_stream(
    messages: list[dict],
    model: str,
    max_tokens: int,
    temperature: float,
    *,
    api_key: str = "",
) -> AsyncGenerator[dict, None]:
    try:
        import anthropic
    except ImportError:
        yield {"type": "delta", "text": "[Anthropic SDK not installed]"}
        return

    key = api_key or settings.ANTHROPIC_API_KEY
    if not key:
        yield {"type": "delta", "text": "[Anthropic API key not configured]"}
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
            yield {"type": "delta", "text": text}
        try:
            final = await stream.get_final_message()
            usage = getattr(final, "usage", None)
            if usage:
                yield {
                    "type": "usage",
                    "prompt_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                    "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0),
                }
        except Exception as exc:  # noqa: BLE001
            logger.debug("anthropic.usage_unavailable: %s", exc)


# ── Gemini ────────────────────────────────────────────────────────

async def _gemini_stream(
    messages: list[dict],
    model: str,
    max_tokens: int,
    temperature: float,
    *,
    api_key: str = "",
) -> AsyncGenerator[dict, None]:
    try:
        import google.generativeai as genai
    except ImportError:
        yield {"type": "delta", "text": "[google-generativeai SDK not installed]"}
        return

    key = api_key or settings.GEMINI_API_KEY
    if not key:
        yield {"type": "delta", "text": "[Gemini API key not configured]"}
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
    last_chunk = None
    async for chunk in response:
        if chunk.text:
            yield {"type": "delta", "text": chunk.text}
        last_chunk = chunk
    try:
        meta = getattr(last_chunk, "usage_metadata", None) if last_chunk else None
        if meta:
            yield {
                "type": "usage",
                "prompt_tokens": int(getattr(meta, "prompt_token_count", 0) or 0),
                "completion_tokens": int(getattr(meta, "candidates_token_count", 0) or 0),
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug("gemini.usage_unavailable: %s", exc)


# ── Ollama ────────────────────────────────────────────────────────

async def _ollama_stream(
    messages: list[dict],
    model: str,
    max_tokens: int,
    temperature: float,
) -> AsyncGenerator[dict, None]:
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
                yield {"type": "delta", "text": f"[Ollama error: {response.status_code}]"}
                return
            async for line in response.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    content = data.get("message", {}).get("content", "")
                    if content:
                        yield {"type": "delta", "text": content}
                    if data.get("done"):
                        # Ollama returns prompt_eval_count + eval_count in the final chunk.
                        prompt_t = int(data.get("prompt_eval_count", 0) or 0)
                        completion_t = int(data.get("eval_count", 0) or 0)
                        if prompt_t or completion_t:
                            yield {
                                "type": "usage",
                                "prompt_tokens": prompt_t,
                                "completion_tokens": completion_t,
                            }
                        break
                except json.JSONDecodeError:
                    continue


# ── OpenAI-compatible tool loop (MiniMax / DeepSeek / Qwen API …) ──


def _format_empty_stream_diagnostic(
    provider_label: str,
    finish_reason: str | None,
    completion_tokens: int,
    reasoning_chars: int,
) -> str:
    """Build a human-readable error message for the case where the OpenAI-
    compat stream ended without producing any visible content or tool
    calls. The message is consumed by callers (news sentiment scorer,
    discussion synthesizer) which forward it into their warning logs.
    """
    parts = [f"{provider_label} returned no content"]
    if finish_reason:
        parts.append(f"finish_reason={finish_reason}")
    if completion_tokens:
        parts.append(f"completion_tokens={completion_tokens}")
    if reasoning_chars:
        parts.append(
            f"reasoning_content={reasoning_chars} chars "
            "(thinking model burned the budget on chain-of-thought; "
            "switch to a non-thinking variant or raise max_tokens)"
        )
    elif finish_reason == "length":
        parts.append("(hit max_tokens before any content; raise max_tokens)")
    elif finish_reason == "content_filter":
        parts.append("(blocked by safety filter)")
    return "; ".join(parts)


_TOOL_RESULT_LIST_KEYS = (
    "rows", "quotes", "items", "contracts", "peers",
    "top_buyers", "top_sellers", "headlines",
)

# SSE-facing tool_result summary caps. The summary frame is display /
# client-side data only (the model's own context is capped separately
# via `_cap_tool_result`), so tools whose result the frontend must
# parse in full (run_screener → ScreenerPage NLQueryBar renders the
# rows) get a higher cap; everything else keeps the 2000-char default.
_TOOL_SUMMARY_DEFAULT_LIMIT = 2000
_TOOL_SUMMARY_LIMITS = {"run_screener": 24000}


def _tool_summary_limit(name: str) -> int:
    """Per-tool SSE summary cap. Strips the `mcp__fincept__` prefix so
    the Claude Agent path ("mcp__fincept__run_screener") and the
    OpenAI-compat path ("run_screener") share one table."""
    short = (name or "").rsplit("__", 1)[-1]
    return _TOOL_SUMMARY_LIMITS.get(short, _TOOL_SUMMARY_DEFAULT_LIMIT)


def _cap_tool_result(name: str, result_str: str, limit: int) -> str:
    """Shrink an oversized tool result before it is appended to `convo`
    and re-sent on every subsequent tool iteration.

    Only fires when `result_str` exceeds `limit` chars — small / medium
    results pass through untouched (the great majority: most tools
    already cap their own output). For the few large, unbounded shapes
    (`get_institutional_history` / `get_margin_history` 90-day series,
    `compare_quotes`) the result is a JSON object with a big top-level
    list; that list is reduced to head + tail with the original count
    recorded under `_truncated`, mirroring the head/tail pattern
    `run_backtest` already uses. All scalar / summary fields (count,
    avg, …) are preserved so the model keeps the aggregate signal plus
    the most-recent and earliest data points — enough to cite a trend.
    Falls back to a head+tail char slice for non-JSON / still-oversized
    payloads so the snippet stays readable rather than cut mid-structure.
    Caller skips this for error payloads (kept full for diagnostics).
    """
    if len(result_str) <= limit:
        return result_str

    head_n, tail_n = 15, 5
    try:
        data = json.loads(result_str)
    except (json.JSONDecodeError, ValueError):
        data = None

    if isinstance(data, dict):
        trimmed: dict[str, int] = {}
        for k, v in list(data.items()):
            if k in _TOOL_RESULT_LIST_KEYS and isinstance(v, list) and len(v) > head_n + tail_n:
                data[k] = v[:head_n] + v[-tail_n:]
                trimmed[k] = len(v)
        if trimmed:
            data["_truncated"] = {
                "reason": f"result exceeded {limit} chars; large lists kept as "
                          f"head{head_n}+tail{tail_n}",
                "original_counts": trimmed,
            }
            capped = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            if len(capped) <= limit:
                return capped
            result_str = capped  # smaller now; char-slice the remainder below

    # Fallback: keep a parseable-looking head + tail char slice.
    keep = max(limit - 200, limit // 2)
    cut_head = keep * 3 // 4
    cut_tail = keep - cut_head
    return (
        result_str[:cut_head]
        + f" …[{len(result_str) - keep} chars truncated by tool-result cap] "
        + result_str[-cut_tail:]
    )


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
        # Ask OpenAI-compat servers to include usage in the final stream chunk
        # (OpenAI / Groq / DeepSeek / OpenRouter all honor this). MiniMax may
        # ignore it; that's fine, we just won't get usage from them.
        payload["stream_options"] = {"include_usage": True}

        # accumulator: index → {id, name, arguments_str}
        pending: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        assistant_text_parts: list[str] = []
        any_text = False
        prompt_tokens_seen = 0
        completion_tokens_seen = 0
        # Reasoning models (MiniMax-M2, DeepSeek-R1, etc.) stream chain-of-
        # thought via `delta.reasoning_content` separate from `delta.content`.
        # We don't yield reasoning as visible tokens (would leak CoT into
        # transcripts and confuse JSON parsers downstream), but we count
        # bytes so an empty-content stream can be diagnosed: "model burned
        # N chars on reasoning before hitting max_tokens" is a very different
        # failure than "model returned nothing at all".
        reasoning_chars_seen = 0

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
                        # Final chunk often has empty choices but a usage block.
                        usage = obj.get("usage")
                        if usage:
                            prompt_tokens_seen = int(usage.get("prompt_tokens", 0) or 0)
                            completion_tokens_seen = int(usage.get("completion_tokens", 0) or 0)
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
                        rc = delta.get("reasoning_content")
                        if rc:
                            reasoning_chars_seen += len(rc)
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

        # Emit per-iteration usage (PR #216): when the model goes
        # through a tool loop (max_turns iterations of LLM call →
        # tool exec → LLM call), each iteration consumes its own
        # prompt + completion tokens. Previously we only emitted on
        # the FINAL iteration, so a 5-turn run silently dropped 4
        # iterations of input cost — UsageCard reported a fraction
        # of true spend. Now every iteration that produced usage
        # emits the event; the consumer (run_round /
        # synthesize_conclusion) sums them.
        if prompt_tokens_seen or completion_tokens_seen:
            yield {
                "type": "usage",
                "prompt_tokens": prompt_tokens_seen,
                "completion_tokens": completion_tokens_seen,
            }

        # Stream ended with no visible content AND no tool intent — the
        # caller can't proceed but previously got no signal at all (raw=""
        # silently). Surface a diagnostic so news_sentiment.llm_error /
        # discussion synthesizer logs explain the failure mode instead of
        # the generic "parse_failed raw=''" downstream symptom. The most
        # common cause is a thinking model (MiniMax-M2, DeepSeek-R1)
        # emitting reasoning_content until max_tokens is exhausted; the
        # admin's fix is either to switch to a non-thinking variant or
        # raise the per-call max_tokens budget.
        if not any_text and not pending and finish_reason != "tool_calls":
            yield {
                "type": "error",
                "message": _format_empty_stream_diagnostic(
                    provider_label,
                    finish_reason,
                    completion_tokens_seen,
                    reasoning_chars_seen,
                ),
            }
            return

        if finish_reason != "tool_calls" or not pending:
            # Natural stop, length cap, or content filter.
            return

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

            _sum_cap = _tool_summary_limit(name)
            yield {
                "type": "tool_result",
                "id": tool_id, "name": name,
                "summary": result_str if len(result_str) <= _sum_cap else result_str[:_sum_cap] + " …[truncated]",
                "is_error": is_error,
            }
            convo.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "name": name,
                # Cap oversized results before they accumulate in `convo`
                # (re-sent on every later tool turn). Errors stay full for
                # diagnostics. See _cap_tool_result.
                "content": result_str if is_error
                else _cap_tool_result(name, result_str, settings.TOOL_RESULT_MAX_CHARS),
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
                                _tname = pending.get(tool_use_id, "")
                                yield {
                                    "type": "tool_result",
                                    "id": tool_use_id,
                                    "name": _tname,
                                    "summary": _truncate(raw, _tool_summary_limit(_tname)),
                                    "is_error": bool(getattr(block, "is_error", False)),
                                }
                elif isinstance(msg, ResultMessage):
                    if msg.is_error:
                        yield {"type": "error", "message": msg.result or "claude_agent returned error"}
                    break  # Terminal
    except Exception as exc:
        logger.exception("claude_agent stream failed")
        yield {"type": "error", "message": str(exc)}
