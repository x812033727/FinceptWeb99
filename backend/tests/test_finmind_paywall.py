"""Tests for `data.tw.finmind_paywall` — the shared FinMind tier /
paywall detector. Pulled out of `test_ingest_revenue_tw_task` in
PR #188 so the helpers can be exercised independent of any one
cron's wiring."""
from __future__ import annotations

import httpx

from data.tw.finmind_paywall import (
    extract_body_message,
    is_paywall_response,
    looks_like_paywall,
)


# ── helpers ───────────────────────────────────────────────────────

def _http_status_error(status_code: int, body: dict | str | None) -> httpx.HTTPStatusError:
    """Build an `httpx.HTTPStatusError` whose `response.json()` returns
    `body` (or raises if `body` is a non-JSON string)."""
    request = httpx.Request("GET", "https://api.finmindtrade.com/api/v4/data")
    if isinstance(body, dict):
        response = httpx.Response(status_code, request=request, json=body)
    else:
        response = httpx.Response(
            status_code, request=request,
            content=(body or "").encode("utf-8"),
            headers={"Content-Type": "text/html"},
        )
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=request, response=response,
    )


# ── looks_like_paywall (string-level matcher) ────────────────────

def test_matches_canonical_finmind_paywall_phrase():
    """The actual wording FinMind returned in 2026-04 when the
    `TaiwanStockMonthRevenue` market-wide query went sponsor-only.
    Pin verbatim so a future copy-paste from a real failure still
    matches."""
    assert looks_like_paywall(
        "Your level is register. Please update your user level. "
        "Detail information: https://finmindtrade.com/analysis/#/Sponsor/sponsor"
    )


def test_matcher_is_case_insensitive():
    assert looks_like_paywall("YOUR LEVEL IS REGISTER")
    assert looks_like_paywall("Your Level Is Register")
    assert looks_like_paywall("PLEASE UPDATE YOUR USER LEVEL")


def test_matches_sponsor_phrasing():
    """Two wide nets — `requires paid` and `sponsor` — to catch
    rewording that doesn't match the literal 'level is register'
    sentence."""
    assert looks_like_paywall("This dataset requires paid sponsor")
    assert looks_like_paywall("Sponsor only access")
    assert looks_like_paywall("Premium subscriber sponsor tier")


def test_does_not_match_generic_400_or_outage_messages():
    assert not looks_like_paywall("")
    assert not looks_like_paywall("Bad request")
    assert not looks_like_paywall("invalid date format")
    assert not looks_like_paywall("token expired")
    assert not looks_like_paywall("Internal Server Error")
    assert not looks_like_paywall("quota exhausted")


# ── extract_body_message ──────────────────────────────────────────

def test_extracts_msg_field_from_finmind_response():
    exc = _http_status_error(400, {"status": 400, "msg": "Your level is register"})
    assert extract_body_message(exc) == "Your level is register"


def test_extracts_message_or_detail_when_msg_absent():
    """FinMind has used `msg` historically; FastAPI-style backends use
    `message` or `detail`. Tolerate all three."""
    exc1 = _http_status_error(400, {"message": "fallback wording"})
    exc2 = _http_status_error(400, {"detail": "another wording"})
    assert extract_body_message(exc1) == "fallback wording"
    assert extract_body_message(exc2) == "another wording"


def test_returns_empty_string_when_body_is_html():
    """FinMind sometimes serves a plain HTML 502 page (gateway down)
    that won't decode as JSON. The detector returns '' which then
    falls through to the transient-failure path (auto-backoff)."""
    exc = _http_status_error(502, "<html>Gateway timeout</html>")
    assert extract_body_message(exc) == ""


def test_returns_empty_string_for_non_http_exceptions():
    """Pure transport / timeout errors aren't paywalls — extractor
    returns '' so the caller's main `_format_error` path handles
    them as outages."""
    assert extract_body_message(RuntimeError("boom")) == ""
    assert extract_body_message(httpx.ConnectTimeout("timeout")) == ""


def test_returns_empty_when_body_dict_lacks_known_keys():
    exc = _http_status_error(400, {"unknown_key": "something"})
    assert extract_body_message(exc) == ""


# ── is_paywall_response (composed canonical check) ───────────────

def test_composed_check_matches_real_paywall():
    """The shape we'd see in production from a tier-mismatched
    request: HTTP 400 + the canonical phrase in `msg`."""
    exc = _http_status_error(400, {
        "status": 400,
        "msg": "Your level is register. Please update your user level.",
    })
    assert is_paywall_response(exc) is True


def test_composed_check_returns_false_for_real_outage():
    """503 with no body → not a paywall, just an outage. Caller
    should treat as transient."""
    exc = _http_status_error(503, "Service Unavailable")
    assert is_paywall_response(exc) is False


def test_composed_check_returns_false_for_non_http_error():
    assert is_paywall_response(RuntimeError("boom")) is False
    assert is_paywall_response(httpx.ConnectTimeout("dns failed")) is False


def test_composed_check_returns_false_for_400_without_paywall_wording():
    """A 400 from FinMind that's actually a malformed-request error
    (e.g. bad date format) must not be misclassified as paywall —
    that would silence a real bug."""
    exc = _http_status_error(400, {"msg": "invalid date format: expected YYYY-MM-DD"})
    assert is_paywall_response(exc) is False
