"""
WebFetch tool — allowlist-gated, SSRF-hardened, body-capped.

The allowlist is set via `CLAUDE_AGENT_WEBFETCH_ALLOWLIST` (comma-separated hosts).
When empty, every fetch is refused — fail-closed.
"""
import ipaddress
import json
import logging
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx
from claude_agent_sdk import SdkMcpTool, tool

from config import settings

logger = logging.getLogger(__name__)

_MAX_BODY = 5 * 1024 * 1024  # 5 MB
_MAX_REDIRECTS = 2


class _TextExtractor(HTMLParser):
    """Strip HTML tags + script/style contents into plain text."""
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        return " ".join(self._chunks)


def _host_is_internal(host: str) -> bool:
    """True if host resolves to a loopback / link-local / RFC1918 address."""
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved or ip.is_multicast:
                return True
        return False
    except (socket.gaierror, ValueError):
        # DNS failed or bad IP — treat as unsafe
        return True


def make_web_tools() -> list[SdkMcpTool]:

    @tool(
        "web_fetch",
        "Fetch a URL from the allowlist and return its text content. "
        "Only HTTPS on allowlisted hosts; max 5 MB body; max 2 redirects; "
        "HTML is extracted to plain text.",
        {"url": str, "max_chars": int},
    )
    async def web_fetch(args: dict[str, Any]) -> dict:
        url = args.get("url", "")
        max_chars = min(int(args.get("max_chars", 8000)), 20_000)
        parsed = urlparse(url)
        allow = settings.claude_agent_webfetch_hosts

        if parsed.scheme != "https":
            return _err("Only HTTPS is permitted")
        if not parsed.hostname:
            return _err("URL missing host")
        host = parsed.hostname.lower()
        if not allow:
            return _err("No hosts on allowlist (CLAUDE_AGENT_WEBFETCH_ALLOWLIST). Fetch refused.")
        if not any(host == h or host.endswith("." + h) for h in allow):
            return _err(f"Host '{host}' not on allowlist")
        if _host_is_internal(host):
            return _err("Host resolves to an internal/private address")

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=_MAX_REDIRECTS,
                timeout=15.0,
                headers={"User-Agent": "FinceptAgent/1.0"},
            ) as client:
                r = await client.get(url)
                # Cap body
                body = r.content[:_MAX_BODY]
                ctype = (r.headers.get("content-type") or "").lower()
                if "html" in ctype:
                    parser = _TextExtractor()
                    parser.feed(body.decode(r.encoding or "utf-8", errors="replace"))
                    text = parser.text()
                else:
                    text = body.decode(r.encoding or "utf-8", errors="replace")
                text = text[:max_chars]
                return _ok({"url": str(r.url), "status": r.status_code, "text": text})
        except Exception as exc:
            logger.warning("web_fetch tool failed: %s: %s", url, exc)
            return _err(str(exc))

    return [web_fetch]


def _ok(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": json.dumps({"error": msg})}], "is_error": True}
