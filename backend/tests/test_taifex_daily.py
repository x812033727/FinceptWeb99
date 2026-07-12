"""Unit tests for the TAIFEX daily futures/options downloaders in
data.tw.taifex_connector — parsing + near-month/day-session selection
over a mocked HTTP layer (real CSV shape, no network)."""
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from data.tw import taifex_connector as tf

_FUT_HEADER = (
    "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,"
    "成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,"
    "是否因訊息面暫停交易,交易時段,價差對單式委託成交量"
)
# TX near month (一般 + 盤後), TX far month, MTX near month.
_FUT_CSV = "\n".join([
    _FUT_HEADER,
    "2026/01/05,TX,202601  ,29888,30487,29850,30313,834,2.83%,95320,30309,75674,30309,30312,30487,26277,,一般,",
    "2026/01/05,TX,202601  ,29505,29796,29463,29776,297,1.01%,58378,-,-,29772,29778,29796,26277,,盤後,",
    "2026/01/05,TX,202602  ,29976,30572,29943,30410,854,2.89%,1504,30401,3076,30402,30409,30572,26305,,一般,",
    "2026/01/05,MTX,202601  ,29890,30489,29852,30315,836,2.84%,120000,30310,50000,30310,30313,30489,26277,,一般,",
])

_OPT_HEADER = (
    "交易日期,契約,到期月份(週別),履約價,買賣權,開盤價,最高價,最低價,收盤價,成交量,"
    "結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,"
    "是否因訊息面暫停交易,交易時段,漲跌價,漲跌%,契約到期日,"
)
_OPT_CSV = "\n".join([
    _OPT_HEADER,
    "2026/01/05,TXO,202601W1,25400.0000,賣權,0.2,0.4,0.2,0.3,46,0.2,453,0.2,0.3,10.5,0.2,,一般,0.1,50%,20260107",
    "2026/01/05,TXO,202601W1,25400.0000,賣權,-,-,-,-,0,-,-,-,-,-,-,,盤後,-,-,20260107",
    "2026/01/05,TXO,202602,25400.0000,賣權,1.0,1.2,1.0,1.1,5,1.0,10,1.0,1.1,20,1.0,,一般,0.1,10%,20260218",
])


def test_num_handles_missing_and_commas():
    assert tf._num("95,320") == "95320"
    assert tf._num("-") is None
    assert tf._num("") is None
    assert tf._num(None) is None
    assert tf._num(" 30309 ") == "30309"


def test_parse_market_rows_header_keyed():
    rows = tf._parse_market_rows(_FUT_CSV)
    assert len(rows) == 4
    assert rows[0]["契約"] == "TX"
    assert rows[0]["交易時段"] == "一般"


@pytest.mark.asyncio
async def test_get_futures_daily_near_month_day_session():
    with patch.object(tf, "_download_taifex_csv",
                      new=AsyncMock(return_value=_FUT_CSV)):
        out = await tf.get_futures_daily(date(2026, 1, 5), date(2026, 1, 5))

    by_contract = {r["futures_id"]: r for r in out}
    # One row per contract: near month (202601), day session.
    assert set(by_contract) == {"TX", "MTX"}
    tx = by_contract["TX"]
    assert tx["date"] == "2026-01-05"
    assert tx["close"] == "30313"          # 一般 close, not 盤後 (29776)
    assert tx["open_interest"] == "75674"
    assert tx["settlement_price"] == "30309"
    # Far month (202602) must not have been chosen.
    assert tx["volume"] == "95320"


@pytest.mark.asyncio
async def test_get_option_daily_shapes_and_near_month():
    with patch.object(tf, "_download_taifex_csv",
                      new=AsyncMock(return_value=_OPT_CSV)):
        out = await tf.get_option_daily(date(2026, 1, 5), date(2026, 1, 5))

    # One near-month row per (contract, strike, call_put); the 202601W1
    # 一般 row wins over its 盤後 twin and the 202602 far month.
    assert len(out) == 1
    r = out[0]
    assert r["option_id"] == "TXO"
    assert r["strike_price"] == "25400.0000"
    assert r["call_put"] == "賣權"
    assert r["close"] == "0.3"
    assert r["open_interest"] == "453"


@pytest.mark.asyncio
async def test_get_futures_daily_empty_on_reversed_range():
    out = await tf.get_futures_daily(date(2026, 1, 6), date(2026, 1, 5))
    assert out == []
