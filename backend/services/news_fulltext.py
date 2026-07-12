"""Full-text article extraction (G5 news enhancement, phase 2).

Given a first-party article URL (from a direct-publisher feed), fetch
the page and extract the main body with trafilatura. Used by the
`enrich_news_fulltext` task to backfill `news_articles.body`.

Politeness (the article pages are third-party servers, not our API):
  - **robots.txt**: honoured per-domain via `urllib.robotparser`, cached
    with a short TTL. Disallowed → skip (return None), don't fetch.
  - **Per-domain throttle**: at most one fetch per domain every
    `_DOMAIN_MIN_INTERVAL_S`, enforced with an asyncio lock + last-hit
    timestamp so concurrent callers serialise per host.
  - Identifying User-Agent, bounded timeout, capped body length.

Failure is never fatal: any error (block, timeout, paywall, empty
extract) returns None and the caller records the attempt so it isn't
retried forever.
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36 FinceptNewsBot/1.0"
)
_FETCH_TIMEOUT_S = 15.0
_MAX_BODY_CHARS = 20_000
_DOMAIN_MIN_INTERVAL_S = 2.0
_ROBOTS_TTL_S = 3600.0

# Per-domain throttle state: domain -> (last_fetch_monotonic).
_last_hit: dict[str, float] = {}
_domain_locks: dict[str, asyncio.Lock] = {}
# Per-domain robots cache: domain -> (RobotFileParser|None, cached_at).
_robots_cache: dict[str, tuple[RobotFileParser | None, float]] = {}


def _domain(url: str) -> str:
    return urlsplit(url).netloc.lower()


def _domain_lock(domain: str) -> asyncio.Lock:
    lock = _domain_locks.get(domain)
    if lock is None:
        lock = asyncio.Lock()
        _domain_locks[domain] = lock
    return lock


async def _robots_allows(client: httpx.AsyncClient, url: str) -> bool:
    """Check robots.txt for `url`'s domain (cached). Fail-open on a
    fetch error (missing/unreadable robots.txt conventionally means
    'allowed'); fail-closed only on an explicit Disallow."""
    domain = _domain(url)
    now = time.monotonic()
    cached = _robots_cache.get(domain)
    if cached is not None and (now - cached[1]) < _ROBOTS_TTL_S:
        rp = cached[0]
    else:
        rp = RobotFileParser()
        try:
            scheme = urlsplit(url).scheme or "https"
            resp = await client.get(f"{scheme}://{domain}/robots.txt")
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp = None  # no robots.txt → allow all
        except Exception:
            rp = None
        _robots_cache[domain] = (rp, now)
    if rp is None:
        return True
    return rp.can_fetch(_UA, url)


async def _throttle(domain: str) -> None:
    """Ensure ≥ _DOMAIN_MIN_INTERVAL_S between fetches to one domain."""
    async with _domain_lock(domain):
        last = _last_hit.get(domain)
        now = time.monotonic()
        if last is not None:
            wait = _DOMAIN_MIN_INTERVAL_S - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)
        _last_hit[domain] = time.monotonic()


async def extract_body(url: str) -> str | None:
    """Fetch + extract the main article text at `url`, or None on any
    failure / disallow / empty extract. Body is trimmed to
    `_MAX_BODY_CHARS`."""
    if not url:
        return None
    domain = _domain(url)
    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT_S, follow_redirects=True,
            headers={"User-Agent": _UA},
        ) as client:
            if not await _robots_allows(client, url):
                log.info("news_fulltext.robots_disallow",
                         extra={"domain": domain})
                return None
            await _throttle(domain)
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        log.info("news_fulltext.fetch_failed",
                 extra={"url": url[:120], "error": str(exc)[:120]})
        return None

    body = _extract_html(html)
    if not body:
        return None
    body = body.strip()
    if len(body) > _MAX_BODY_CHARS:
        body = body[:_MAX_BODY_CHARS].rstrip() + "…"
    return body or None


def _extract_html(html: str) -> str | None:
    """Isolated so tests can exercise extraction without the network.
    trafilatura is imported lazily to keep this module import-light."""
    try:
        import trafilatura
    except ImportError:  # pragma: no cover - dependency always present in prod
        log.warning("news_fulltext.trafilatura_missing")
        return None
    try:
        return trafilatura.extract(
            html, include_comments=False, include_tables=False,
        )
    except Exception as exc:
        log.info("news_fulltext.extract_failed", extra={"error": str(exc)[:120]})
        return None
