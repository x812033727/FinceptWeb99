"""Unit test for the 三大法人-期貨 pivot in
data.tw.taifex_connector.get_futures_institutional (mocked HTTP)."""
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from data.tw import taifex_connector as tf

_HEADER = (
    "日期,商品名稱,身份別,多方交易口數,多方交易契約金額(千元),空方交易口數,"
    "空方交易契約金額(千元),多空交易口數淨額,多空交易契約金額淨額(千元),"
    "多方未平倉口數,多方未平倉契約金額(千元),空方未平倉口數,空方未平倉契約金額(千元),"
    "多空未平倉口數淨額,多空未平倉契約金額淨額(千元)"
)
_CSV = "\n".join([
    _HEADER,
    "2026/01/05,臺股期貨,自營商,14288,85968754,15008,90325189,-720,-4356434,4134,25072809,8614,52243162,-4480,-27170353",
    "2026/01/05,臺股期貨,投信,502,3044217,45,272710,457,2771507,32819,198942269,5326,32285147,27493,166657122",
    "2026/01/05,臺股期貨,外資及陸資,73660,441906276,75638,453896818,-1978,-11990543,23747,143995715,48848,296127008,-25101,-152131293",
    # An unmapped product → must be skipped (not emitted under a name).
    "2026/01/05,某冷門期貨,外資及陸資,1,2,3,4,-2,-2,10,20,30,40,-20,-20",
])


@pytest.mark.asyncio
async def test_get_futures_institutional_pivots_three_investors():
    with patch.object(tf, "_download_taifex_csv",
                      new=AsyncMock(return_value=_CSV)):
        rows = await tf.get_futures_institutional(date(2026, 1, 5), date(2026, 1, 5))

    # Only the mapped TX product survives; unmapped 某冷門期貨 dropped.
    assert len(rows) == 1
    tx = rows[0]
    assert tx["date"] == "2026-01-05"
    assert tx["futures_id"] == "TX"
    # Foreign (外資及陸資) OI.
    assert tx["long_open_interest_balance_volume_foreign_investment"] == "23747"
    assert tx["short_open_interest_balance_volume_foreign_investment"] == "48848"
    # Investment trust (投信).
    assert tx["long_open_interest_balance_volume_investment_trust"] == "32819"
    # Dealer (自營商).
    assert tx["short_open_interest_balance_volume_dealer"] == "8614"


@pytest.mark.asyncio
async def test_get_futures_institutional_empty_on_reversed_range():
    assert await tf.get_futures_institutional(date(2026, 1, 6), date(2026, 1, 5)) == []
