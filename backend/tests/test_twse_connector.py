"""
Unit tests for data.tw.twse_connector.

The connector funnels every HTTP call through `_get`, so mocking that
single helper covers all the high-level methods. Pure helpers
(_tw_num / _tw_int / _twse_date / _to_utc8_ts) are tested directly.

The hardest path to verify is the ROC-calendar date conversion in
get_daily_ohlcv (`113/04/01` → `2024-04-01` via the +1911 rule) —
TW's official API returns dates in the Republic-of-China calendar
which is one of the most regression-prone parts of the connector.
"""
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

import data.tw.twse_connector as twse


# ── Pure helpers ──────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("1,234.56", 1234.56),
    ("1234", 1234.0),
    ("0", 0.0),
    ("-1.5", -1.5),
    (None, None),
    ("", None),
    ("--", None),  # TWSE uses "--" for missing values
    ("N/A", None),
])
def test_tw_num_handles_comma_thousands_and_garbage(raw, expected):
    assert twse._tw_num(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("1,234,567", 1234567),
    ("0", 0),
    (None, 0),  # _tw_int returns 0 for None (volume default)
    ("", 0),
    ("--", 0),
])
def test_tw_int_handles_comma_thousands_and_returns_zero_on_garbage(raw, expected):
    assert twse._tw_int(raw) == expected


def test_twse_date_formats_yyyymmdd():
    assert twse._twse_date(date(2024, 4, 1)) == "20240401"
    assert twse._twse_date(date(2024, 12, 31)) == "20241231"


def test_twse_date_defaults_to_today():
    today = twse._twse_date()
    assert len(today) == 8 and today.isdigit()


def test_to_utc8_ts_parses_slash_format_with_taiwan_timezone():
    out = twse._to_utc8_ts("2024/04/01")
    # Output should be ISO with +08:00 offset
    assert out.startswith("2024-04-01T")
    assert out.endswith("+08:00")


def test_to_utc8_ts_parses_dash_format():
    out = twse._to_utc8_ts("2024-04-01")
    assert out.startswith("2024-04-01T")
    assert out.endswith("+08:00")


def test_to_utc8_ts_returns_input_unchanged_on_unparseable_string():
    assert twse._to_utc8_ts("not-a-date") == "not-a-date"


@pytest.mark.parametrize("raw,expected", [
    # ROC slash with leading zeros stripped
    ("113/04/01",   "2024-04-01"),
    ("113/12/31",   "2024-12-31"),
    # ROC slash without zero-pad
    ("113/4/1",     "2024-04-01"),
    # ISO already
    ("2024-04-01",  "2024-04-01"),
    # ROC compact 7-digit
    ("1130401",     "2024-04-01"),
    # ISO compact 8-digit
    ("20240401",    "2024-04-01"),
    # Bad inputs
    (None,          None),
    ("",            None),
    ("not-a-date",  None),
    ("113/04",      None),  # incomplete
])
def test_tw_date_to_iso_handles_every_known_twse_format(raw, expected):
    """TWSE rotates date formats across endpoints. The helper must
    accept every documented variant + reject junk so callers can
    drop unfilterable rows cleanly without surprise crashes."""
    assert twse._tw_date_to_iso(raw) == expected


# ── Helper: install _get mock ─────────────────────────────────────

def install_get(payload):
    """Patch twse._get to return `payload` regardless of url/params.
    Returns the AsyncMock so callers can inspect call args."""
    mock = AsyncMock(return_value=payload)
    return patch.object(twse, "_get", new=mock), mock


# ── Symbol lists ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_all_twse_symbols_returns_list_when_endpoint_returns_list():
    patcher, _ = install_get([{"Code": "2330", "Name": "台積電"}])
    with patcher:
        out = await twse.get_all_twse_symbols()
    assert out == [{"Code": "2330", "Name": "台積電"}]


@pytest.mark.asyncio
async def test_get_all_twse_symbols_returns_empty_when_endpoint_returns_non_list():
    """TWSE occasionally returns {"stat":"OK","data":[]} envelope or
    just a string error message — connector defaults to []."""
    patcher, _ = install_get({"stat": "OK"})
    with patcher:
        out = await twse.get_all_twse_symbols()
    assert out == []


# ── get_daily_ohlcv: ROC calendar conversion ─────────────────────

@pytest.mark.asyncio
async def test_get_daily_ohlcv_converts_roc_date_to_ce():
    """TWSE returns dates as `<roc_year>/MM/DD` where roc_year + 1911 =
    western year. 113 → 2024."""
    payload = [
        {
            "日期": "113/04/01",
            "開盤價": "780.00", "最高價": "790.00", "最低價": "775.00",
            "收盤價": "785.00", "成交股數": "12,345,678",
        }
    ]
    patcher, _ = install_get(payload)
    with patcher:
        bars = await twse.get_daily_ohlcv("2330")

    assert len(bars) == 1
    assert bars[0]["time"] == "2024-04-01"
    assert bars[0]["open"] == 780.0
    assert bars[0]["close"] == 785.0
    assert bars[0]["volume"] == 12345678


@pytest.mark.asyncio
async def test_get_daily_ohlcv_skips_malformed_rows_silently():
    """A bad row in the middle of the response shouldn't break the
    whole response — the connector wraps each row in try/except."""
    payload = [
        {"日期": "113/04/01", "開盤價": "10", "最高價": "11", "最低價": "9", "收盤價": "10.5", "成交股數": "100"},
        {"日期": "garbage"},  # bad row
        {"日期": "113/04/02", "開盤價": "10.5", "最高價": "11.5", "最低價": "10", "收盤價": "11", "成交股數": "200"},
    ]
    patcher, _ = install_get(payload)
    with patcher:
        bars = await twse.get_daily_ohlcv("2330")
    times = [b["time"] for b in bars]
    assert "2024-04-01" in times
    assert "2024-04-02" in times


@pytest.mark.asyncio
async def test_get_daily_ohlcv_passes_date_and_stockno_params():
    patcher, mock = install_get([])
    with patcher:
        await twse.get_daily_ohlcv("2330", date(2024, 4, 1))
    # mock was awaited with the date in YYYYMMDD form and the stock no.
    _, kwargs = mock.call_args
    assert kwargs["params"]["date"] == "20240401"
    assert kwargs["params"]["stockNo"] == "2330"


# ── get_realtime_quote ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_realtime_quote_matches_on_chinese_key():
    """STOCK_DAY_ALL responses use 證券代號 / 證券名稱 (legacy schema)."""
    payload = [
        {"證券代號": "2330", "證券名稱": "台積電", "收盤價": "785.00",
         "漲跌價差": "+5.00", "成交股數": "12,345,678",
         "開盤價": "780.00", "最高價": "790.00", "最低價": "775.00"},
    ]
    patcher, _ = install_get(payload)
    with patcher:
        q = await twse.get_realtime_quote("2330")

    assert q is not None
    assert q["symbol"] == "2330"
    assert q["name_zh"] == "台積電"
    assert q["close"] == 785.0
    assert q["change"] == 5.0
    assert q["volume"] == 12345678


@pytest.mark.asyncio
async def test_get_realtime_quote_matches_on_english_key():
    """Newer endpoints use Code / Name / ClosingPrice."""
    payload = [
        {"Code": "2330", "Name": "TSMC", "ClosingPrice": "785.00",
         "Change": "5", "TradeVolume": "1000", "OpeningPrice": "780",
         "HighestPrice": "790", "LowestPrice": "775"},
    ]
    patcher, _ = install_get(payload)
    with patcher:
        q = await twse.get_realtime_quote("2330")
    assert q is not None
    assert q["close"] == 785.0
    assert q["volume"] == 1000


@pytest.mark.asyncio
async def test_get_realtime_quote_returns_none_when_symbol_missing():
    patcher, _ = install_get([{"Code": "2330"}])
    with patcher:
        q = await twse.get_realtime_quote("9999")
    assert q is None


@pytest.mark.asyncio
async def test_get_realtime_quote_returns_none_on_non_list_payload():
    """TWSE error responses come back as a dict envelope; the
    connector must not blow up trying to iterate it."""
    patcher, _ = install_get({"stat": "OK", "data": []})
    with patcher:
        q = await twse.get_realtime_quote("2330")
    assert q is None


# ── Institutional / margin ────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_institutional_parses_legacy_envelope():
    """The legacy `www.twse.com.tw/fund/T86` endpoint returns
    `{stat, fields, data}` with positional rows. Connector must unwrap
    via `_unwrap_legacy_table` and produce the canonical output shape."""
    payload = {
        "stat": "OK",
        "date": "20260430",
        "fields": [
            "證券代號", "證券名稱",
            "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)",
            "外陸資買賣超股數(不含外資自營商)",
            "外資自營商買進股數", "外資自營商賣出股數", "外資自營商買賣超股數",
            "投信買進股數", "投信賣出股數", "投信買賣超股數",
            "自營商買賣超股數",
            "自營商買進股數(自行買賣)", "自營商賣出股數(自行買賣)", "自營商買賣超股數(自行買賣)",
            "自營商買進股數(避險)", "自營商賣出股數(避險)", "自營商買賣超股數(避險)",
            "三大法人買賣超股數",
        ],
        "data": [
            [
                "2330", "台積電",
                "1,000,000", "500,000", "500,000",
                "0", "0", "0",
                "200,000", "100,000", "100,000",
                "30,000",
                "50,000", "20,000", "30,000",
                "0", "0", "0",
                "630,000",
            ],
        ],
    }
    patcher, _ = install_get(payload)
    with patcher:
        rows = await twse.get_institutional()

    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "2330"
    assert r["name_zh"] == "台積電"
    assert r["fini_buy"] == 1_000_000
    assert r["fini_sell"] == 500_000
    assert r["sitc_buy"] == 200_000
    assert r["sitc_sell"] == 100_000
    assert r["dealer_buy"] == 50_000
    assert r["dealer_sell"] == 20_000


@pytest.mark.asyncio
async def test_get_institutional_returns_empty_on_holiday():
    """Weekend / holiday queries come back as `stat != "OK"` (e.g.
    "很抱歉，沒有符合條件的資料!"). Must yield [] instead of raising
    so the cron records ok=true row_count=0 and doesn't arm backoff."""
    payload = {
        "stat": "很抱歉，沒有符合條件的資料!",
        "date": "20260503",  # Sunday
    }
    patcher, _ = install_get(payload)
    with patcher:
        rows = await twse.get_institutional()
    assert rows == []


@pytest.mark.asyncio
async def test_get_institutional_accepts_legacy_dict_array_passthrough():
    """Defensive backwards compat: if a future TWSE rev returns a
    plain list of dicts again, the parser still handles it (mirrors
    the pre-2026-04 OpenAPI shape)."""
    payload = [
        {
            "證券代號": "2330", "證券名稱": "台積電",
            "外陸資買進股數(不含外資自營商)": "1,000,000",
            "外陸資賣出股數(不含外資自營商)": "500,000",
            "投信買進股數": "200,000", "投信賣出股數": "100,000",
            "自營商買進股數(自行買賣)": "50,000", "自營商賣出股數(自行買賣)": "20,000",
        },
    ]
    patcher, _ = install_get(payload)
    with patcher:
        rows = await twse.get_institutional()

    assert len(rows) == 1
    assert rows[0]["fini_buy"] == 1_000_000


def test_unwrap_legacy_table_handles_malformed_payloads():
    """Defensive parsing: every shape the legacy site can plausibly
    return must produce [] rather than raise."""
    assert twse._unwrap_legacy_table(None) == []
    assert twse._unwrap_legacy_table([1, 2, 3]) == []
    assert twse._unwrap_legacy_table({}) == []
    assert twse._unwrap_legacy_table({"stat": "OK"}) == []
    assert twse._unwrap_legacy_table({"stat": "OK", "fields": "bad", "data": []}) == []
    # short row vs long fields — pair what we can, drop the tail
    assert twse._unwrap_legacy_table({
        "stat": "OK",
        "fields": ["a", "b", "c"],
        "data": [["1", "2"]],
    }) == [{"a": "1", "b": "2"}]


@pytest.mark.asyncio
async def test_get_margin_parses_chinese_field_names():
    payload = [
        {
            "股票代號": "2330", "名稱": "台積電",
            "融資買進": "1,000", "融資餘額": "5,000",
            "融券賣出": "200", "融券餘額": "1,000",
        },
    ]
    patcher, _ = install_get(payload)
    with patcher:
        rows = await twse.get_margin()

    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "2330"
    assert r["margin_purchase"] == 1000
    assert r["margin_balance"] == 5000
    assert r["short_sale"] == 200


# ── TAIEX index ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_taiex_returns_latest_record():
    payload = [
        {"時間": "13:30", "指數": "20,000.00", "漲跌點數": "100.00"},
        {"時間": "13:35", "指數": "20,050.00", "漲跌點數": "150.00"},
    ]
    patcher, _ = install_get(payload)
    with patcher:
        out = await twse.get_taiex()
    assert out["index"] == "TAIEX"
    assert out["value"] == 20050.0
    assert out["change"] == 150.0
    assert out["time"] == "13:35"


@pytest.mark.asyncio
async def test_get_taiex_returns_empty_dict_on_empty_payload():
    patcher, _ = install_get([])
    with patcher:
        out = await twse.get_taiex()
    assert out == {}


# ── Valuation ratios ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_valuation_ratios_parses_positional_row():
    """BWIBBU_d row format: [日期, 殖利率(%), 股利年度, 本益比, 股價淨值比]"""
    payload = {"data": [
        ["2024-04-01", "2.5", "2023", "20.5", "5.2"],
        ["2024-04-02", "2.6", "2023", "21.0", "5.3"],
    ]}
    patcher, _ = install_get(payload)
    with patcher:
        out = await twse.get_valuation_ratios("2330")

    assert out["pe_ratio"] == 21.0
    assert out["pb_ratio"] == 5.3
    assert out["dividend_yield"] == 2.6
    assert out["fetched_at"] == "2024-04-02"


@pytest.mark.asyncio
async def test_get_valuation_ratios_returns_empty_when_no_data():
    patcher, _ = install_get({"data": []})
    with patcher:
        out = await twse.get_valuation_ratios("2330")
    assert out == {}


@pytest.mark.asyncio
async def test_get_valuation_ratios_handles_short_rows_without_crashing():
    """If TWSE adds/removes columns the row may not have all five
    positions — connector should populate what it can and None the rest."""
    payload = {"data": [["2024-04-01", "2.5"]]}  # only 2 columns
    patcher, _ = install_get(payload)
    with patcher:
        out = await twse.get_valuation_ratios("2330")
    assert out["dividend_yield"] == 2.5
    assert out["pe_ratio"] is None
    assert out["pb_ratio"] is None


# ── Buyback / Delisting (Phase 1B corporate-action) ──────────────


@pytest.mark.asyncio
async def test_get_buyback_announcements_normalises_dates_and_keys():
    """t187ap43_L returns Chinese keys + ROC dates. Connector must
    normalise to a stable English schema with ISO dates so the Phase B
    selfcrawl wrapper can rename to FinMind's column_map keys without
    re-parsing per-call."""
    payload = [
        {
            "公司代號":     "2330",
            "公司名稱":     "台積電",
            "董事會決議日期": "113/04/15",
            "預定買回起日":  "113/04/16",
            "預定買回迄日":  "113/06/15",
            "預定買回股數":  "10,000,000",
            "已買回股數":    "5,000,000",
            "已買回平均價格": "850.0",
        },
    ]
    patcher, _ = install_get(payload)
    with patcher:
        rows = await twse.get_buyback_announcements()

    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "2330"
    assert r["name_zh"] == "台積電"
    assert r["announced_at"] == "2024-04-15"
    assert r["started_at"] == "2024-04-16"
    assert r["ended_at"] == "2024-06-15"
    assert r["plan_shares"] == 10_000_000
    assert r["actual_shares"] == 5_000_000
    assert r["avg_price"] == 850.0


@pytest.mark.asyncio
async def test_get_buyback_announcements_drops_rows_missing_symbol():
    """A junk row without `公司代號` must be silently dropped — the
    `(symbol, announced_at)` PK on tw_buyback would NOT NULL-fail on
    insert, so dropping at the connector layer keeps the chunk healthy."""
    payload = [
        {"公司代號": "2330", "董事會決議日期": "113/04/15"},
        {"公司代號": "",     "董事會決議日期": "113/04/15"},  # drop
        {"公司代號": None,   "董事會決議日期": "113/04/15"},  # drop
    ]
    patcher, _ = install_get(payload)
    with patcher:
        rows = await twse.get_buyback_announcements()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "2330"


@pytest.mark.asyncio
async def test_get_buyback_announcements_returns_empty_on_non_list():
    patcher, _ = install_get({"some": "object", "not": "a list"})
    with patcher:
        rows = await twse.get_buyback_announcements()
    assert rows == []


@pytest.mark.asyncio
async def test_get_delisted_companies_normalises_dates_and_keys():
    payload = [
        {
            "公司代號":     "2520",
            "公司名稱":     "冠德",
            "終止上市日期": "2023-08-22",
            "終止上市原因": "申請終止上市",
        },
    ]
    patcher, _ = install_get(payload)
    with patcher:
        rows = await twse.get_delisted_companies()
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "2520"
    assert r["delisted_at"] == "2023-08-22"
    assert "終止" in r["reason"]


@pytest.mark.asyncio
async def test_get_delisted_companies_handles_roc_compact_date():
    """Some t187ap08_L revisions return the date as 7-digit ROC
    compact (e.g. `1120822`). Make sure the date helper picks it up."""
    payload = [
        {
            "公司代號":     "2520",
            "公司名稱":     "冠德",
            "終止上市日期": "1120822",
            "終止上市原因": "申請終止上市",
        },
    ]
    patcher, _ = install_get(payload)
    with patcher:
        rows = await twse.get_delisted_companies()
    assert rows[0]["delisted_at"] == "2023-08-22"


# ── Securities lending (Phase 1C TWT93U) ─────────────────────────


@pytest.mark.asyncio
async def test_get_securities_lending_daily_normalises_to_english_keys():
    """TWT93U returns Chinese keys + comma-separated numeric strings.
    Connector must translate to a stable English schema so the Phase
    B selfcrawl wrapper doesn't have to reach into the TWSE column
    naming."""
    payload = [
        {
            "證券代號":         "2330",
            "證券名稱":         "台積電",
            "借券賣出成交數量": "1,234,567",
            "借券平均費率":     "0.85",
        },
        {
            "證券代號":         "2317",
            "證券名稱":         "鴻海",
            "借券賣出成交數量": "500,000",
            "借券平均費率":     "1.20",
        },
    ]
    patcher, _ = install_get(payload)
    with patcher:
        rows = await twse.get_securities_lending_daily(date(2024, 1, 2))

    assert len(rows) == 2
    by_id = {r["symbol"]: r for r in rows}
    r = by_id["2330"]
    assert r["name_zh"] == "台積電"
    assert r["volume"] == 1_234_567
    assert r["fee_rate"] == 0.85


@pytest.mark.asyncio
async def test_get_securities_lending_daily_drops_rows_missing_symbol():
    """The PK on tw_securities_lending NOT-NULLs symbol; drop at the
    connector layer keeps the chunk healthy."""
    payload = [
        {"證券代號": "2330", "借券賣出成交數量": "1", "借券平均費率": "0.5"},
        {"證券代號": "",     "借券賣出成交數量": "1", "借券平均費率": "0.5"},
        {"證券代號": None,   "借券賣出成交數量": "1", "借券平均費率": "0.5"},
    ]
    patcher, _ = install_get(payload)
    with patcher:
        rows = await twse.get_securities_lending_daily(date(2024, 1, 2))
    assert len(rows) == 1
    assert rows[0]["symbol"] == "2330"


@pytest.mark.asyncio
async def test_get_securities_lending_daily_unwraps_legacy_envelope():
    """Legacy `www.twse.com.tw` endpoints sometimes return the
    `{stat, fields, data}` envelope rather than a flat list — the
    helper must accept both shapes via _unwrap_legacy_table."""
    payload = {
        "stat": "OK",
        "fields": ["證券代號", "證券名稱", "借券賣出成交數量", "借券平均費率"],
        "data": [
            ["2330", "台積電", "1,234,567", "0.85"],
        ],
    }
    patcher, _ = install_get(payload)
    with patcher:
        rows = await twse.get_securities_lending_daily(date(2024, 1, 2))
    assert len(rows) == 1
    assert rows[0]["symbol"] == "2330"
    assert rows[0]["volume"] == 1_234_567


@pytest.mark.asyncio
async def test_get_securities_lending_daily_returns_empty_on_holiday():
    """`stat != "OK"` (TWSE's response on a non-trading day) → []."""
    payload = {"stat": "MS_DATA_NOT_FOUND", "fields": [], "data": []}
    patcher, _ = install_get(payload)
    with patcher:
        rows = await twse.get_securities_lending_daily(date(2024, 1, 1))
    assert rows == []
