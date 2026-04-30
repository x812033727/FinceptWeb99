"""
Python execution tool — subprocess sandbox with resource limits.

Defense-in-depth (NOT a full container):
- Fresh `python -I -S` subprocess: ignore env, skip site-packages
- RLIMIT_CPU, RLIMIT_AS, RLIMIT_FSIZE=0 (no disk writes), RLIMIT_NPROC
- Wall-clock timeout via asyncio.wait_for (belt + braces vs RLIMIT_CPU)
- stdin carries JSON envelope of user code + inputs
- Runner stub imports only a safe allow-list of modules

For production we still recommend running the backend in an unprivileged
container with seccomp. This sandbox is a second layer, not the primary
boundary.
"""
import asyncio
import json
import logging
import os
import sys
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from config import settings

if os.name == "posix":
    import resource
else:
    resource = None

logger = logging.getLogger(__name__)

# Runner executed inside the subprocess. Reads a JSON envelope from stdin,
# exposes allowed modules, runs the user code, captures stdout + result.
_RUNNER = r"""
import io, json, sys, contextlib
envelope = json.loads(sys.stdin.read())
code = envelope["code"]
inputs = envelope.get("inputs", {})

allowed = {}
for mod_name in ("math", "statistics", "decimal", "itertools", "functools", "datetime", "json", "re"):
    allowed[mod_name] = __import__(mod_name)
for mod_name in ("numpy", "pandas"):
    try:
        allowed[mod_name] = __import__(mod_name)
    except ImportError:
        pass

# Shadow builtins that enable escape vectors
unsafe = {"open", "input", "eval", "exec", "compile", "__import__"}
safe_builtins = {k: v for k, v in __builtins__.__dict__.items() if k not in unsafe}

buf = io.StringIO()
result = None
error = None
try:
    g = {"__builtins__": safe_builtins, "inputs": inputs, **allowed}
    with contextlib.redirect_stdout(buf):
        exec(code, g)
    if "result" in g:
        try:
            result = json.loads(json.dumps(g["result"], default=str))
        except Exception:
            result = str(g["result"])
except Exception as exc:
    error = f"{type(exc).__name__}: {exc}"

print(json.dumps({"stdout": buf.getvalue()[:4000], "result": result, "error": error}, default=str))
"""


def _preexec():
    """Apply RLIMITs before the subprocess calls exec()."""
    if resource is None:
        return
    mem_bytes = settings.CLAUDE_AGENT_PYTHON_MEM_MB * 1024 * 1024
    cpu_s = settings.CLAUDE_AGENT_PYTHON_CPU_S
    resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (16, 16))
    except (ValueError, OSError):
        pass  # Not supported on every platform


def make_python_tools() -> list[SdkMcpTool]:

    @tool(
        "python_exec",
        "Execute a short Python snippet in a sandbox (no network, no disk write, "
        "CPU/memory limited). Assign a JSON-serialisable value to the `result` variable "
        "to return it. `inputs` is available as a dict. Allowed modules: math, statistics, "
        "decimal, itertools, functools, datetime, json, re, numpy, pandas.",
        {"code": str, "inputs": dict},
    )
    async def python_exec(args: dict[str, Any]) -> dict:
        code = args.get("code", "")
        inputs = args.get("inputs", {}) or {}
        envelope = json.dumps({"code": code, "inputs": inputs})
        wall = settings.CLAUDE_AGENT_PYTHON_CPU_S + 3

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-S", "-c", _RUNNER,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=_preexec if os.name == "posix" else None,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(envelope.encode()),
                    timeout=wall,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return _err("python_exec timeout exceeded")

            if proc.returncode != 0:
                return _err(f"exit code {proc.returncode}: {(stderr or b'')[:500].decode(errors='replace')}")
            try:
                return _ok(json.loads(stdout.decode()))
            except json.JSONDecodeError:
                return _err("runner produced non-JSON output")
        except Exception as exc:
            logger.warning("python_exec tool failed: %s", exc)
            return _err(str(exc))

    return [python_exec]


def _ok(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}]}


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": json.dumps({"error": msg})}], "is_error": True}
