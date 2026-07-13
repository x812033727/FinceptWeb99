"""Gateway providers — drive the SUBSCRIPTION CLIs/SDK on the host.

None of these take an API key: they inherit the host's subscription
credentials (Claude Max OAuth in ~/.claude/.credentials.json, Codex
OAuth in ~/.codex/auth.json, Antigravity Google OAuth). This mirrors
the Ti studio pattern (studio/experts.py, studio/providers.py).

Each provider is an async generator of plain text deltas; app.py wraps
them into OpenAI-compatible SSE. R2a is text-only — the fincept tool
loop lands in R4 via a reverse HTTP callback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator

log = logging.getLogger("llm_gateway.providers")


class ProviderError(RuntimeError):
    """Provider could not produce output (missing CLI/SDK, auth, spawn)."""


# ── claude_sub — claude-agent-sdk, subscription OAuth ─────────────

async def claude_sub_stream(
    messages: list[dict], model: str, max_turns: int = 4,
) -> AsyncGenerator[str, None]:
    """Stream assistant text from Claude via claude-agent-sdk. The SDK
    spawns the bundled Claude CLI which reads ~/.claude/.credentials.json
    — we deliberately never set ANTHROPIC_API_KEY, so billing goes to the
    Max subscription (see the Ti ti-autopilot.service note)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        # Loud guard: a stray key would silently switch to API billing.
        log.warning("claude_sub: ANTHROPIC_API_KEY is set — unsetting for subscription billing")
        os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            PermissionResultAllow,
            TextBlock,
        )
    except ImportError as exc:  # pragma: no cover - host must install SDK
        raise ProviderError(f"claude-agent-sdk not installed: {exc}") from exc

    system, chat = _split_system(messages)
    prompt = _flatten_chat(chat)

    async def _auto_allow(tool_name, tool_input, context):
        return PermissionResultAllow()

    options = ClaudeAgentOptions(
        system_prompt=system or None,
        permission_mode="default",
        can_use_tool=_auto_allow,
        model=model or None,
        max_turns=max_turns,
    )
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            yield block.text
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"claude_sub stream failed: {exc}") from exc


# ── codex_sub — `codex exec` non-interactive, subscription OAuth ──

async def codex_sub_stream(
    messages: list[dict], model: str,
) -> AsyncGenerator[str, None]:
    system, chat = _split_system(messages)
    prompt = (system + "\n\n" if system else "") + _flatten_chat(chat)
    argv = [
        os.environ.get("CODEX_BIN", "codex"), "exec", "--json", "--ephemeral",
        # The gateway uses codex purely as a text LLM from its own working
        # dir (/opt/llm-gateway, not a git repo). Without this, `codex exec`
        # aborts with "Not inside a trusted directory".
        "--skip-git-repo-check",
        "--sandbox", "read-only", "-c", 'approval_policy="never"',
        "--color", "never",
    ]
    if model:
        argv += ["--model", model]
    argv.append("-")
    async for text in _spawn_jsonl(argv, prompt, _codex_extract):
        yield text


# ── agy — Antigravity CLI, Google OAuth ──────────────────────────

async def agy_stream(
    messages: list[dict], model: str,
) -> AsyncGenerator[str, None]:
    system, chat = _split_system(messages)
    prompt = (system + "\n\n" if system else "") + _flatten_chat(chat)
    # `agy -p/--print <prompt>` runs one prompt non-interactively and prints
    # the response — the prompt is the flag's ARGUMENT, not stdin.
    argv = [os.environ.get("AGY_BIN", "agy")]
    if model:
        argv += ["--model", model]
    argv += ["-p", prompt]
    # agy prints the response to stdout (not JSONL) — stream raw lines; no
    # stdin (the prompt already rode in as the -p argument).
    async for text in _spawn_lines(argv, ""):
        yield text


# ── helpers ──────────────────────────────────────────────────────

def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    system_parts, chat = [], []
    for m in messages:
        if m.get("role") == "system":
            system_parts.append(str(m.get("content", "")))
        else:
            chat.append(m)
    return "\n\n".join(system_parts), chat


def _flatten_chat(chat: list[dict]) -> str:
    lines = []
    for m in chat:
        role = m.get("role", "user")
        content = m.get("content", "")
        prefix = "User" if role == "user" else "Assistant"
        lines.append(f"{prefix}: {content}")
    lines.append("Assistant:")
    return "\n".join(lines)


def _codex_extract(obj: dict) -> str | None:
    """Pull assistant text out of a codex JSONL event. Codex emits
    `{"type":"item.completed","item":{"type":"agent_message","text":...}}`
    plus deltas; we surface completed agent messages."""
    item = obj.get("item") or obj.get("msg") or {}
    if isinstance(item, dict) and item.get("type") in ("agent_message", "assistant_message"):
        return item.get("text") or item.get("content")
    if obj.get("type") == "assistant" and obj.get("text"):
        return obj["text"]
    return None


async def _spawn_jsonl(argv, prompt, extract) -> AsyncGenerator[str, None]:
    proc = await _spawn(argv)
    proc.stdin.write(prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()
    async for line in proc.stdout:
        line = line.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = extract(obj)
        if text:
            yield text
    await _drain_end(proc, argv)


async def _spawn_lines(argv, prompt) -> AsyncGenerator[str, None]:
    proc = await _spawn(argv)
    proc.stdin.write(prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()
    async for line in proc.stdout:
        chunk = line.decode("utf-8", "replace")
        if chunk:
            yield chunk
    await _drain_end(proc, argv)


async def _spawn(argv):
    try:
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )
    except FileNotFoundError as exc:
        raise ProviderError(f"{argv[0]} not found on PATH") from exc


async def _drain_end(proc, argv):
    rc = await proc.wait()
    if rc != 0:
        err = (await proc.stderr.read()).decode("utf-8", "replace")[:300]
        raise ProviderError(f"{argv[0]} exited {rc}: {err}")
