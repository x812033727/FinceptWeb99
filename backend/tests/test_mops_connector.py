"""Unit tests for `data.tw.mops_connector.get_monthly_revenue_recent`.

The connector hits the new MOPS SPA backend at
`mops.interinfo.com.tw:8443/mops/api/t05st10_ifrs`. We mock httpx
at the connector's import-site so no network is touched and pin both
the canonical happy-path shape and every defensive branch (HTTP
errors, MOPS `code != 200`, nested `result.data` vs flat list,
ROC-vs-CE year handling, comma-separated revenue strings, missing /
malformed dates).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import data.tw.mops_connector as mops


# ── helpers ───────────────────────────────────────────────────────

def _mock_post(json_body: dict | None = None, *, status_code: int = 200,
               raise_exc: BaseException | None = None):
    """Patch `httpx.AsyncClient` at the connector's import-site.
    Returns a context manager that, when entered, makes
    `AsyncClient().post(...)` produce the configured response (or raise
    `raise_exc` instead of returning)."""
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(return_value=json_body or {})

    if raise_exc is not None:
        post = AsyncMock(side_effect=raise_exc)
    else:
        post = AsyncMock(return_value=response)

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.post = post
    return patch.object(mops.httpx, "AsyncClient", lambda **_: fake_client), post


# ── happy path ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_parses_canonical_payload_with_english_keys():
    """The cleanest shape the SPA backend returns: `result.data` is a
    list of dicts with English keys + ROC year."""
    payload = {
        "code": 200,
        "message": "ok",
        "result": {
            "data": [
                {"year": "114", "month": "04", "revenue": "1,234,567",
                 "revenueYoy": "12.5", "revenueMom": "-3.2"},
                {"year": "114", "month": "03", "revenue": "1,100,000",
                 "revenueYoy": "8.0", "revenueMom": "1.1"},
            ],
        },
    }
    patcher, _ = _mock_post(payload)
    with patcher:
        rows = await mops.get_monthly_revenue_recent("2330")

    assert len(rows) == 2
    assert rows[0]["symbol"] == "2330"
    # ROC 114 → CE 2025
    assert rows[0]["date"] == "2025-04-01"
    assert rows[0]["revenue"] == 1_234_567
    assert rows[0]["revenue_yoy"] == 12.5
    assert rows[0]["revenue_mom"] == -3.2


@pytest.mark.asyncio
async def test_accepts_flat_result_list_shape():
    """Some report variants put rows directly under `result` instead
    of `result.data`. Connector tolerates both."""
    payload = {
        "code": 200,
        "result": [
            {"year": "2025", "month": "04", "revenue": "500000"},
        ],
    }
    patcher, _ = _mock_post(payload)
    with patcher:
        rows = await mops.get_monthly_revenue_recent("2454")
    # year was already CE — no ROC offset applied
    assert rows == [{
        "symbol": "2454", "date": "2025-04-01",
        "revenue": 500_000, "revenue_yoy": None, "revenue_mom": None,
    }]


@pytest.mark.asyncio
async def test_handles_yymm_combined_year_month_field():
    """The legacy MOPS report shape sometimes carries (year, month) as
    a single `yymm` string (`"11404"` = ROC 114 / month 04)."""
    payload = {
        "code": 200,
        "result": {"data": [{"yymm": "11404", "revenue": "9,000"}]},
    }
    patcher, _ = _mock_post(payload)
    with patcher:
        rows = await mops.get_monthly_revenue_recent("1101")
    assert len(rows) == 1
    assert rows[0]["date"] == "2025-04-01"
    assert rows[0]["revenue"] == 9_000


# ── error / no-data branches ──────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_list_on_406_no_data():
    """`code: 406` is MOPS for "查無相符資料" — newly-listed companies
    or ones that haven't filed for the period yet. NOT an error,
    return [] silently (no log spam)."""
    payload = {"code": 406, "message": "查無相符資料", "result": None}
    patcher, _ = _mock_post(payload)
    with patcher:
        rows = await mops.get_monthly_revenue_recent("9999")
    assert rows == []


@pytest.mark.asyncio
async def test_returns_empty_list_on_http_500():
    payload = {"code": 500, "message": "傳入參數異常", "result": None}
    patcher, _ = _mock_post(payload, status_code=500)
    with patcher:
        rows = await mops.get_monthly_revenue_recent("2330")
    assert rows == []


@pytest.mark.asyncio
async def test_returns_empty_list_on_connection_error():
    """Per-symbol timeouts / connect errors must not propagate. The
    cron iterates over 1700+ symbols; one transport failure must not
    abort the batch."""
    patcher, _ = _mock_post(raise_exc=httpx.ConnectTimeout("timeout"))
    with patcher:
        rows = await mops.get_monthly_revenue_recent("2330")
    assert rows == []


@pytest.mark.asyncio
async def test_returns_empty_list_on_html_response_with_200():
    """Under load MOPS sometimes returns the SPA HTML even with HTTP
    200 — `r.json()` raises ValueError. Don't crash."""
    response = MagicMock()
    response.status_code = 200
    response.json = MagicMock(side_effect=ValueError("not json"))

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.post = AsyncMock(return_value=response)
    with patch.object(mops.httpx, "AsyncClient", lambda **_: fake_client):
        rows = await mops.get_monthly_revenue_recent("2330")
    assert rows == []


@pytest.mark.asyncio
async def test_skips_unparseable_rows_in_otherwise_good_payload():
    """One bad row shouldn't poison the whole batch."""
    payload = {
        "code": 200,
        "result": {"data": [
            {"year": "114", "month": "04", "revenue": "1,000"},
            {"year": "bad", "month": "??"},
            {"year": "114", "month": "03", "revenue": "950"},
            "not-a-dict",
            {"year": "114", "month": "13", "revenue": "1"},  # invalid month
        ]},
    }
    patcher, _ = _mock_post(payload)
    with patcher:
        rows = await mops.get_monthly_revenue_recent("2330")
    assert len(rows) == 2
    assert {r["date"] for r in rows} == {"2025-04-01", "2025-03-01"}


# ── small parsing helpers ─────────────────────────────────────────

def test_to_int_strips_commas_and_handles_blanks():
    assert mops._to_int("1,234,567") == 1_234_567
    assert mops._to_int("0") == 0
    assert mops._to_int("") is None
    assert mops._to_int("--") is None
    assert mops._to_int(None) is None
    assert mops._to_int("not a number") is None


def test_to_float_handles_pct_suffix_and_blanks():
    assert mops._to_float("12.5") == 12.5
    assert mops._to_float("12.5%") == 12.5
    assert mops._to_float("-3.2") == -3.2
    assert mops._to_float("") is None
    assert mops._to_float("N/A") is None


def test_normalize_row_returns_none_on_missing_date_pieces():
    out = mops._normalize_row("2330", {"revenue": "1000"})
    assert out is None


def test_normalize_row_keeps_ce_years_unchanged():
    """Year >= 1900 means CE — don't accidentally double-offset to
    something like 3936 (1925 + 1911)."""
    out = mops._normalize_row("2330", {"year": "2025", "month": "04",
                                       "revenue": "1,000"})
    assert out is not None
    assert out["date"] == "2025-04-01"


def test_normalize_row_handles_alternate_revenue_field_names():
    out = mops._normalize_row("2330", {
        "year": "114", "month": "04",
        "currentMonthRevenue": "5,000",
        "yoyChange": "10",
        "momChange": "-2",
    })
    assert out is not None
    assert out["revenue"] == 5_000
    assert out["revenue_yoy"] == 10.0
    assert out["revenue_mom"] == -2.0
