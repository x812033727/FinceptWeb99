"""`mops_connector.get_buyback_summary` — the redirectToOld →
legacy ajax_t35sc09 two-hop flow and the HTML table parser.

Fixture rows mirror the live table verified 2026-07-28 (column
layout: 序號/代號/名稱/決議日/目的code/金額上限/預定股數/價格下限/
價格上限/期間起/期間迄/執行完畢/達標/已買回股數/...).
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from data.tw.mops_connector import (
    _parse_buyback_table,
    _roc_compact,
    _roc_slash_to_iso,
    get_buyback_summary,
)

_HTML = """
<table><tr><td>上市公司買回自己公司股份彙總統計表</td></tr>
<tr><td>日期：114/01/01~115/07/28</td><td>出表日：115/07/28</td></tr>
<tr><th>序號</th><th>公司代號</th><th>公司名稱</th><th>董事會決議日期</th>
<th>買回目的</th><th>金額上限</th><th>預定買回股數</th><th>下限</th><th>上限</th>
<th>起</th><th>迄</th><th>完畢</th><th>達標</th><th>已買回股數</th></tr>
<tr><td>1</td><td>2850</td><td>新產</td><td>114/04/07</td><td>1</td>
<td>13,040,757,237</td><td>5,000,000</td><td>70.70</td><td>168.45</td>
<td>114/04/08</td><td>114/06/07</td><td>Y</td><td></td><td></td>
<td></td><td>0.00</td><td>0</td><td>0.00</td><td>0.00</td><td>說明</td></tr>
<tr><td>2</td><td>2524</td><td>京城</td><td>114/04/08</td><td>3</td>
<td>16,866,697,000</td><td>10,000,000</td><td>38.50</td><td>70.00</td>
<td>114/04/09</td><td>114/06/06</td><td>Y</td><td></td><td>3,957,000</td>
<td>3,957,000</td><td>39.57</td><td>195,694,642</td><td>49.46</td><td>1.07</td><td>說明</td></tr>
</table>
"""


def test_roc_helpers():
    assert _roc_compact(date(2026, 7, 1)) == "1150701"
    assert _roc_slash_to_iso("114/04/07") == "2025-04-07"
    assert _roc_slash_to_iso("") is None
    assert _roc_slash_to_iso("garbage") is None


def test_parse_buyback_table_extracts_data_rows_only():
    rows = _parse_buyback_table(_HTML)
    assert len(rows) == 2
    first, second = rows
    assert first["symbol"] == "2850"
    assert first["announce_date"] == "2025-04-07"
    assert first["method"] == 1
    assert first["purpose"] == "轉讓股份予員工"
    assert first["max_shares"] == 5_000_000
    assert first["price_lower"] == 70.70
    assert first["price_upper"] == 168.45
    assert first["period_start"] == "2025-04-08"
    assert first["period_end"] == "2025-06-07"
    assert first["current_shares"] is None  # 空白為尚在執行中
    assert second["method"] == 3
    assert second["purpose"] == "維護公司信用及股東權益"
    assert second["current_shares"] == 3_957_000


def test_parse_buyback_table_tolerates_error_page():
    assert _parse_buyback_table(
        "<table><tr><td>彙總統計表</td></tr>"
        "<tr><td><h4>查無所需資料</h4></td></tr></table>"
    ) == []


@pytest.mark.asyncio
async def test_get_buyback_summary_two_hop_flow():
    """POST redirectToOld per market → GET the returned legacy URL →
    parse. sii+otc rows concatenate."""
    redirect_json = {"code": 200, "result": {"url": "https://mopsov.example/x"}}

    post_mock = AsyncMock(return_value=httpx.Response(
        200, json=redirect_json,
        request=httpx.Request("POST", "https://mops.twse.com.tw/mops/api/redirectToOld"),
    ))
    get_mock = AsyncMock(return_value=httpx.Response(
        200, text=_HTML,
        request=httpx.Request("GET", "https://mopsov.example/x"),
    ))
    with patch("httpx.AsyncClient.post", post_mock), \
         patch("httpx.AsyncClient.get", get_mock):
        rows = await get_buyback_summary(date(2026, 4, 1), date(2026, 7, 28))

    assert post_mock.await_count == 2  # sii + otc
    sent = post_mock.await_args_list[0].kwargs["json"]
    assert sent["apiName"] == "ajax_t35sc09"
    # Compact ROC digits — slashed forms are rejected upstream.
    assert sent["parameters"]["d1"] == "1150401"
    assert sent["parameters"]["d2"] == "1150728"
    assert {c["TYPEK"] for c in
            (call.kwargs["json"]["parameters"] for call in post_mock.await_args_list)
            } == {"sii", "otc"}
    assert len(rows) == 4  # both markets served the same fixture


@pytest.mark.asyncio
async def test_get_buyback_summary_missing_url_skips_market():
    post_mock = AsyncMock(return_value=httpx.Response(
        200, json={"code": 200, "result": {}},
        request=httpx.Request("POST", "https://mops.twse.com.tw/mops/api/redirectToOld"),
    ))
    get_mock = AsyncMock()
    with patch("httpx.AsyncClient.post", post_mock), \
         patch("httpx.AsyncClient.get", get_mock):
        rows = await get_buyback_summary(date(2026, 4, 1), date(2026, 7, 28))
    assert rows == []
    get_mock.assert_not_called()
