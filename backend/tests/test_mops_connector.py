"""
Unit tests for data.tw.mops_connector.

MOPS is the third tier of the TW monthly-revenue waterfall (TWSE →
FinMind → MOPS). It scrapes HTML rather than calling a JSON API, so
the test surface centres on:

  - The CE→ROC year conversion baked into the POST payload
    (year - 1911) and the zero-padded month string.
  - HTML table parsing — `<table class="hasBorder">` gone missing
    (page layout change), rows shorter than 3 cells, and the comma-
    separated revenue numbers MOPS returns.

httpx.AsyncClient is mocked at the module's import-site so no network
is hit and we can shape the HTML body per-test.
"""
from unittest.mock import patch

import pytest

import data.tw.mops_connector as mops


# ── Test doubles ──────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.posts: list[tuple[str, dict, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def post(self, url, data=None, headers=None):
        self.posts.append((url, data or {}, headers or {}))
        return self.response


def install_client(response):
    fake = FakeClient(response)
    return patch.object(mops.httpx, "AsyncClient", lambda **_: fake), fake


# ── Helpers for HTML payloads ────────────────────────────────────

def html_with_revenue_rows(*revenues: str) -> str:
    """Build a minimal MOPS-shaped page with a `<table class="hasBorder">`
    containing one data row per revenue value (column 2 is the revenue
    field per the connector's hard-coded `cells[2]` index)."""
    body_rows = "".join(
        f"<tr><td>2024/04</td><td>foo</td><td>{rev}</td><td>x</td></tr>"
        for rev in revenues
    )
    return (
        "<html><body>"
        "<table class=\"hasBorder\">"
        "<tr><th>月份</th><th>產業</th><th>營收</th><th>備註</th></tr>"
        f"{body_rows}"
        "</table></body></html>"
    )


# ── Payload wiring ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_monthly_revenue_converts_ce_year_to_roc_in_payload():
    """CE year 2024 → ROC 113 in the POST form data; month is the
    zero-padded two-digit string."""
    patcher, fake = install_client(FakeResponse(html_with_revenue_rows("0")))
    with patcher:
        await mops.get_monthly_revenue("2330", 2024, 4)

    _, data, _ = fake.posts[0]
    assert data["year"] == "113"
    assert data["month"] == "04"
    assert data["co_id"] == "2330"


@pytest.mark.asyncio
async def test_get_monthly_revenue_zero_pads_single_digit_month():
    """Months 1-9 must come back as `01`-`09`."""
    patcher, fake = install_client(FakeResponse(html_with_revenue_rows("0")))
    with patcher:
        await mops.get_monthly_revenue("2330", 2024, 7)
    assert fake.posts[0][1]["month"] == "07"


@pytest.mark.asyncio
async def test_get_monthly_revenue_uses_post_with_form_content_type():
    """MOPS rejects requests without the form-encoded Content-Type
    header. Pin it down so a future refactor doesn't drop it."""
    patcher, fake = install_client(FakeResponse(html_with_revenue_rows("0")))
    with patcher:
        await mops.get_monthly_revenue("2330", 2024, 4)
    _, _, headers = fake.posts[0]
    assert headers.get("Content-Type") == "application/x-www-form-urlencoded"


# ── Parsing ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_monthly_revenue_parses_comma_separated_revenue():
    patcher, _ = install_client(FakeResponse(html_with_revenue_rows("12,345,678")))
    with patcher:
        out = await mops.get_monthly_revenue("2330", 2024, 4)

    assert len(out) == 1
    row = out[0]
    assert row["symbol"] == "2330"
    assert row["date"] == "2024-04-01"
    assert row["revenue"] == 12_345_678  # comma stripped, parsed as int
    assert row["source"] == "mops"


@pytest.mark.asyncio
async def test_get_monthly_revenue_returns_zero_for_non_digit_revenue():
    """If the revenue cell contains a placeholder (`-`, blank, or any
    non-digit string), the connector returns 0 rather than raising."""
    patcher, _ = install_client(FakeResponse(html_with_revenue_rows("--")))
    with patcher:
        out = await mops.get_monthly_revenue("2330", 2024, 4)
    assert out[0]["revenue"] == 0


@pytest.mark.asyncio
async def test_get_monthly_revenue_returns_empty_when_table_missing():
    """MOPS occasionally returns an error page or a captcha page that
    has no `<table class="hasBorder">`. Connector returns []."""
    patcher, _ = install_client(FakeResponse("<html><body>session expired</body></html>"))
    with patcher:
        out = await mops.get_monthly_revenue("2330", 2024, 4)
    assert out == []


@pytest.mark.asyncio
async def test_get_monthly_revenue_skips_rows_with_fewer_than_three_cells():
    """A summary or separator row with only 1-2 cells must not crash
    the parser. Real data rows in the same table still flow through."""
    html = (
        "<html><body><table class=\"hasBorder\">"
        "<tr><th>h</th></tr>"
        "<tr><td>summary</td><td>row</td></tr>"  # 2 cells — skipped
        "<tr><td>2024/04</td><td>foo</td><td>1,000</td><td>x</td></tr>"
        "</table></body></html>"
    )
    patcher, _ = install_client(FakeResponse(html))
    with patcher:
        out = await mops.get_monthly_revenue("2330", 2024, 4)
    assert len(out) == 1
    assert out[0]["revenue"] == 1000


@pytest.mark.asyncio
async def test_get_monthly_revenue_handles_multiple_data_rows():
    patcher, _ = install_client(FakeResponse(
        html_with_revenue_rows("1,000", "2,000", "3,000")
    ))
    with patcher:
        out = await mops.get_monthly_revenue("2330", 2024, 4)
    assert [r["revenue"] for r in out] == [1000, 2000, 3000]
    # Every row carries the same date/symbol/source — the table is
    # one stock's history grid.
    assert all(r["symbol"] == "2330" for r in out)
    assert all(r["date"] == "2024-04-01" for r in out)


@pytest.mark.asyncio
async def test_get_monthly_revenue_propagates_http_errors():
    """503 / 4xx from MOPS bubbles up so the service-layer waterfall
    can fall back to its prior tier or surface a 502 to the client."""
    patcher, _ = install_client(FakeResponse("err", status_code=503))
    with patcher, pytest.raises(Exception):
        await mops.get_monthly_revenue("2330", 2024, 4)
