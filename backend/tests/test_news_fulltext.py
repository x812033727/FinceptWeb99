"""Unit tests for `services.news_fulltext` — full-text extraction with
robots + throttle politeness, mocked HTTP (no network).
"""
from __future__ import annotations

import httpx
import pytest

import services.news_fulltext as ft

ARTICLE_HTML = """<html><head><title>t</title></head><body>
<article><h1>台積電法說會</h1>
<p>台積電今日召開法人說明會，管理層表示先進製程需求強勁，全年營收看增。</p>
<p>展望下半年，AI 相關訂單能見度延伸至明年，資本支出維持高檔。</p>
</article></body></html>"""

ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"
ROBOTS_DISALLOW = "User-agent: *\nDisallow: /\n"


class _FakeResp:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err", request=None, response=self)  # type: ignore[arg-type]


class _FakeClient:
    """Maps URL substrings → _FakeResp; records fetched URLs."""

    def __init__(self, routes, calls):
        self._routes = routes
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, *a, **k):
        self._calls.append(url)
        for frag, resp in self._routes.items():
            if frag in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return _FakeResp(404, "")


@pytest.fixture(autouse=True)
def _reset_state():
    ft._last_hit.clear()
    ft._robots_cache.clear()
    ft._domain_locks.clear()
    yield
    ft._last_hit.clear()
    ft._robots_cache.clear()
    ft._domain_locks.clear()


def _patch_client(monkeypatch, routes, calls):
    monkeypatch.setattr(
        ft.httpx, "AsyncClient", lambda *a, **k: _FakeClient(routes, calls)
    )


def test_extract_html_pulls_body_from_real_html():
    body = ft._extract_html(ARTICLE_HTML)
    assert body and "台積電" in body and "先進製程" in body


@pytest.mark.asyncio
async def test_extract_body_success(monkeypatch):
    calls: list[str] = []
    _patch_client(monkeypatch, {
        "robots.txt": _FakeResp(200, ROBOTS_ALLOW),
        "/article": _FakeResp(200, ARTICLE_HTML),
    }, calls)
    body = await ft.extract_body("https://pub.example.com/article/1")
    assert body and "台積電" in body
    assert any("robots.txt" in c for c in calls)  # robots was checked


@pytest.mark.asyncio
async def test_extract_body_respects_robots_disallow(monkeypatch):
    calls: list[str] = []
    _patch_client(monkeypatch, {
        "robots.txt": _FakeResp(200, ROBOTS_DISALLOW),
        "/article": _FakeResp(200, ARTICLE_HTML),
    }, calls)
    body = await ft.extract_body("https://blocked.example.com/article/1")
    assert body is None
    # Article page must NOT be fetched when robots disallows.
    assert not any("/article" in c for c in calls)


@pytest.mark.asyncio
async def test_extract_body_fetch_error_returns_none(monkeypatch):
    calls: list[str] = []
    _patch_client(monkeypatch, {
        "robots.txt": _FakeResp(404, ""),   # no robots → allowed
        "/article": httpx.ConnectError("boom"),
    }, calls)
    assert await ft.extract_body("https://x.example.com/article/1") is None


@pytest.mark.asyncio
async def test_extract_body_truncates(monkeypatch):
    huge = ("<html><body><article>" + "字" * 30_000 +
            "</article></body></html>")
    calls: list[str] = []
    _patch_client(monkeypatch, {
        "robots.txt": _FakeResp(404, ""),
        "/article": _FakeResp(200, huge),
    }, calls)
    body = await ft.extract_body("https://x.example.com/article/1")
    assert body is not None
    assert len(body) <= ft._MAX_BODY_CHARS + 1  # +1 for the ellipsis


@pytest.mark.asyncio
async def test_extract_body_empty_url_returns_none():
    assert await ft.extract_body("") is None
