"""LLM Gateway — host-side sidecar exposing the subscription CLIs
(Claude Max / Codex / Antigravity) to the containerized backend over an
OpenAI-compatible SSE endpoint.

Runs on the HOST (systemd), NOT in Docker, so it can reach
~/.claude/.credentials.json etc. Binds to the docker bridge only and
requires a shared Bearer token — it is never exposed publicly.

Endpoints:
  GET  /health                      liveness
  POST /v1/chat/completions         OpenAI-compat; provider chosen by
                                    the `X-LLM-Provider` header
                                    (claude_sub | codex_sub | agy).

The backend's ai.llm_router `_gateway_stream` is the only client.
"""
from __future__ import annotations

import json
import logging
import os
import time

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from providers import (
    ProviderError,
    agy_stream,
    claude_sub_stream,
    codex_sub_stream,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("llm_gateway")

app = FastAPI(title="LLM Gateway", version="0.1.0")

_TOKEN = os.environ.get("LLM_GATEWAY_TOKEN", "")

_PROVIDERS = {
    "claude_sub": claude_sub_stream,
    "codex_sub": codex_sub_stream,
    "agy": agy_stream,
}


def _require_auth(authorization: str | None) -> None:
    if not _TOKEN:
        return  # unset = local dev, no auth (bind is bridge-only anyway)
    expected = f"Bearer {_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="bad gateway token")


@app.get("/health")
async def health():
    return {"ok": True, "providers": sorted(_PROVIDERS)}


def _sse_chunk(model: str, text: str) -> str:
    payload = {
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    return f"data:{json.dumps(payload, ensure_ascii=False)}\n\n"


def _sse_final(model: str, prompt_tokens: int, completion_chars: int) -> str:
    # We don't have real token counts from the CLIs; report a rough
    # char/4 estimate so the backend's usage plumbing has something.
    payload = {
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_chars // 4,
            "total_tokens": prompt_tokens + completion_chars // 4,
        },
    }
    return f"data:{json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_llm_provider: str | None = Header(default=None),
):
    _require_auth(authorization)
    body = await request.json()
    provider = x_llm_provider or body.get("provider") or ""
    stream_fn = _PROVIDERS.get(provider)
    if stream_fn is None:
        raise HTTPException(status_code=400, detail=f"unknown provider {provider!r}")

    messages = body.get("messages", [])
    model = body.get("model", "")
    prompt_tokens = sum(len(str(m.get("content", ""))) for m in messages) // 4

    # Pre-flight: pull the first chunk before committing the 200 stream.
    # If the provider fails BEFORE any output (auth, quota, spawn), return
    # a real 5xx so the backend's _gateway_stream falls back to the API
    # provider. A failure AFTER first output stays inside the stream.
    agen = stream_fn(messages, model).__aiter__()
    try:
        first = await agen.__anext__()
    except StopAsyncIteration:
        first = None
    except ProviderError as exc:
        log.warning("provider_error_preflight: %s (%s)", exc, provider)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    async def gen():
        completion_chars = 0
        started = time.monotonic()
        if first:
            completion_chars += len(first)
            yield _sse_chunk(model, first)
        try:
            async for text in agen:
                completion_chars += len(text)
                yield _sse_chunk(model, text)
        except ProviderError as exc:
            # Mid-stream failure — output already sent; surface an error
            # marker (backend won't retry, avoiding duplicate output).
            log.warning("provider_error_midstream: %s (%s)", exc, provider)
            yield f"data:{json.dumps({'error': {'message': str(exc), 'provider': provider}})}\n\n"
        finally:
            yield _sse_final(model, prompt_tokens, completion_chars)
            yield "data:[DONE]\n\n"
            log.info("done provider=%s model=%s chars=%d dur=%.1fs",
                     provider, model, completion_chars, time.monotonic() - started)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.exception_handler(HTTPException)
async def _http_exc(_request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": {"message": exc.detail}})
