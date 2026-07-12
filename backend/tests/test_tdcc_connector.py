"""Unit tests for data.tw.tdcc_connector — 集保戶股權分散表 CSV parsing
over a mocked HTTP layer (real CSV shape, no network)."""
from unittest.mock import patch

import pytest

from data.tw import tdcc_connector as tdcc

# BOM + header + two brackets for one stock (real TDCC shape).
_CSV = (
    "﻿資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%\n"
    "20260703,2330,1,12345,6789000,0.26\n"
    "20260703,2330,15,42,9000000000,34.71\n"
    "20260703,,1,0,0,0.00\n"           # blank stock_id → dropped
    "bad,2330,2,1,2,0.01\n"            # bad date → dropped
)


def test_parse_holding_csv_shapes_finmind_raw():
    rows = tdcc._parse_holding_csv(_CSV)
    assert len(rows) == 2
    r = rows[0]
    assert r["date"] == "2026-07-03"          # 20260703 → ISO
    assert r["stock_id"] == "2330"
    assert r["HoldingSharesLevel"] == "1"
    assert r["people"] == "12345"
    assert r["unit"] == "6789000"
    assert r["percent"] == "0.26"


def test_parse_holding_csv_empty_and_bad_header():
    assert tdcc._parse_holding_csv("") == []
    assert tdcc._parse_holding_csv("foo,bar\n1,2") == []


def test_parse_roc_or_greg_day():
    assert tdcc._parse_roc_or_greg_day("20260703") == "2026-07-03"
    assert tdcc._parse_roc_or_greg_day("2026-07-03") is None  # not YYYYMMDD
    assert tdcc._parse_roc_or_greg_day("bad") is None


@pytest.mark.asyncio
async def test_get_holding_shares_per_uses_text_body():
    class _Resp:
        text = _CSV
        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, *a, **k):
            return _Resp()

    with patch.object(tdcc.httpx, "AsyncClient", lambda **k: _Client()):
        rows = await tdcc.get_holding_shares_per()
    assert len(rows) == 2
    assert rows[1]["HoldingSharesLevel"] == "15"
