"""
LLM Router — streams tokens from OpenAI / Anthropic / Gemini / Ollama.
Each provider is tried; caller gets an async generator of text chunks.
"""
from __future__ import annotations
import json
from typing import AsyncGenerator

import httpx

from config import settings

# ── provider dispatch ──────────────────────────────────────────────

async def stream_chat(
    messages: list[dict],
    provider: str | None = None,
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.3,
) -> AsyncGenerator[str, None]:
    """Yield text chunks from the chosen provider's chat completion stream."""
    prov = (provider or settings.DEFAULT_LLM_PROVIDER).lower()

    if prov == "openai":
        async for chunk in _openai_stream(messages, model or "gpt-4o-mini", max_tokens, temperature):
            yield chunk
    elif prov == "anthropic":
        async for chunk in _anthropic_stream(messages, model or "claude-haiku-4-5-20251001", max_tokens, temperature):
            yield chunk
    elif prov == "gemini":
        async for chunk in _gemini_stream(messages, model or "gemini-2.0-flash", max_tokens, temperature):
            yield chunk
    elif prov == "ollama":
        async for chunk in _ollama_stream(messages, model or "llama3.2", max_tokens, temperature):
            yield chunk
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

    # Extract system prompt if present
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

    # Flatten messages to Gemini format
    history = []
    prompt = ""
    for m in messages:
        if m["role"] == "system":
            prompt += m["content"] + "\n\n"
        elif m["role"] == "user":
            history.append({"role": "user", "parts": [m["content"]]})
        elif m["role"] == "assistant":
            history.append({"role": "model", "parts": [m["content"]]})

    # Last user message becomes the actual prompt
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
