"""
Unit tests for data.us.yfinance_connector.

The connector wraps the `yfinance` package and runs every sync call
in a bounded ThreadPoolExecutor. We mock the `yf` module attribute on
the connector so:

  - yf.Ticker(...) returns a FakeTicker with fast_info / info /
    history / financials / balance_sheet / cashflow.
  - yf.download(...) returns a real pandas DataFrame (or empty one)
    so the connector's pandas operations (.dropna, .iloc, .iterrows,
    .columns.get_level_values) exercise their real code paths.

The executor itself is left in place — it just runs the sync mock
synchronously since the mocked surface is non-blocking.
"""
from unittest.mock import patch

import pandas as pd
import pytest

import data.us.yfinance_connector as yfinance


# ── Test doubles ──────────────────────────────────────────────────

class FakeFastInfo:
    """Mocks the yfinance fast_info object. Each attribute can be
    overridden in tests; missing attributes return None via
    _safe_attr's getattr fallback."""
    def __init__(self, **attrs):
        self._attrs = attrs

    def __getattr__(self, name):
        # Raises when missing → _safe_attr catches all Exceptions and
        # returns None, mimicking real fast_info behaviour where some
        # property descriptors throw on network errors.
        if name in self._attrs:
            return self._attrs[name]
        raise AttributeError(name)


class FakeTicker:
    def __init__(self, *, fast_info=None, info=None, history=None,
                 financials=None, balance_sheet=None, cashflow=None):
        self.fast_info = fast_info if fast_info is not None else FakeFastInfo()
        self._info = info
        self._history = history if history is not None else pd.DataFrame()
        self.financials = financials if financials is not None else pd.DataFrame()
        self.balance_sheet = balance_sheet if balance_sheet is not None else pd.DataFrame()
        self.cashflow = cashflow if cashflow is not None else pd.DataFrame()

    @property
    def info(self):
        # info is a property that may raise — mirror that so tests
        # can simulate a failed quoteSummary request.
        if isinstance(self._info, Exception):
            raise self._info
        return self._info

    def history(self, **_kwargs):
        return self._history


# ── Pure helper tests ─────────────────────────────────────────────

def test_safe_attr_returns_value_when_present():
    obj = FakeFastInfo(last_price=204.5)
    assert yfinance._safe_attr(obj, "last_price") == 204.5


def test_safe_attr_returns_none_when_missing():
    obj = FakeFastInfo()
    assert yfinance._safe_attr(obj, "last_price") is None


def test_safe_attr_returns_none_when_getattr_raises():
    """fast_info property descriptors can raise on HTTP errors —
    a plain getattr default catches AttributeError but not the
    network exceptions we actually see in production."""
    class Boom:
        @property
        def thing(self):
            raise RuntimeError("network blip")
    assert yfinance._safe_attr(Boom(), "thing") is None


def test_ts_to_ms_converts_pandas_timestamp():
    ts = pd.Timestamp("2024-01-02T15:00:00Z")
    assert yfinance._ts_to_ms(ts) == int(ts.timestamp() * 1000)


def test_ts_to_ms_returns_none_for_none_input():
    assert yfinance._ts_to_ms(None) is None


# ── get_quote ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_quote_happy_path_uses_fast_info():
    ticker = FakeTicker(
        fast_info=FakeFastInfo(
            last_price=204.5, previous_close=200.0, open=201.0,
            day_high=205.0, day_low=199.5, three_month_average_volume=12345,
            market_cap=3.2e12,
        ),
        info={},
    )
    with patch.object(yfinance.yf, "Ticker", return_value=ticker), \
         patch.object(yfinance.yf, "download") as download_mock:
        q = await yfinance.get_quote("aapl")
        # fast_info gave us a price, so download fallback must not fire.
        assert download_mock.call_count == 0

    assert q["symbol"] == "AAPL"  # uppercased on the way out
    assert q["price"] == 204.5
    assert q["prev_close"] == 200.0
    assert q["change"] == 4.5
    assert q["change_pct"] == 2.25
    assert q["volume"] == 12345
    assert q["market_cap"] == 3.2e12


@pytest.mark.asyncio
async def test_get_quote_falls_back_to_yf_download_when_fast_info_blocked():
    """fast_info returns None for last_price → connector calls
    yf.download to derive price/prev/OHLCV from the most recent bar."""
    ticker = FakeTicker(fast_info=FakeFastInfo(), info={})
    bars = pd.DataFrame({
        "Open":   [200, 201],
        "High":   [205, 206],
        "Low":    [199, 200],
        "Close":  [203, 204],
        "Volume": [1000, 2000],
    })

    with patch.object(yfinance.yf, "Ticker", return_value=ticker), \
         patch.object(yfinance.yf, "download", return_value=bars):
        q = await yfinance.get_quote("AAPL")

    assert q["price"] == 204.0       # last close
    assert q["prev_close"] == 203.0  # second-to-last
    assert q["open"] == 201.0
    assert q["volume"] == 2000


@pytest.mark.asyncio
async def test_get_quote_returns_zeros_when_everything_blocked():
    """fast_info empty + yf.download empty → connector still returns a
    structurally-valid dict with price=0 (caller decides how to handle)."""
    ticker = FakeTicker(fast_info=FakeFastInfo(), info={})
    with patch.object(yfinance.yf, "Ticker", return_value=ticker), \
         patch.object(yfinance.yf, "download", return_value=pd.DataFrame()):
        q = await yfinance.get_quote("X")

    assert q["price"] == 0
    assert q["change"] == 0
    assert q["change_pct"] == 0
    assert q["prev_close"] == 0


@pytest.mark.asyncio
async def test_get_quote_swallows_info_exception_and_uses_fast_info():
    """`.info` raises (Yahoo IP-block on quoteSummary) — connector must
    not propagate the exception, and must still return fast_info data."""
    ticker = FakeTicker(
        fast_info=FakeFastInfo(last_price=300.0, previous_close=290.0),
        info=RuntimeError("quoteSummary blocked"),
    )
    with patch.object(yfinance.yf, "Ticker", return_value=ticker), \
         patch.object(yfinance.yf, "download"):
        q = await yfinance.get_quote("X")
    assert q["price"] == 300.0
    assert q["prev_close"] == 290.0


@pytest.mark.asyncio
async def test_get_quote_logs_and_continues_when_yf_download_raises():
    """If the yf.download fallback itself blows up, the connector must
    still return a dict — not propagate the exception to the caller."""
    ticker = FakeTicker(fast_info=FakeFastInfo(), info={})
    with patch.object(yfinance.yf, "Ticker", return_value=ticker), \
         patch.object(yfinance.yf, "download", side_effect=RuntimeError("download blew up")):
        q = await yfinance.get_quote("X")
    assert q["symbol"] == "X"
    assert q["price"] == 0


# ── get_history ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_history_normalises_pandas_bars_to_dicts():
    idx = pd.DatetimeIndex(["2024-01-02", "2024-01-03"])
    df = pd.DataFrame({
        "Open":   [100.0, 101.0],
        "High":   [102.0, 103.0],
        "Low":    [99.0, 100.0],
        "Close":  [101.0, 102.0],
        "Volume": [10000, 12000],
    }, index=idx)
    ticker = FakeTicker(history=df)

    with patch.object(yfinance.yf, "Ticker", return_value=ticker):
        bars = await yfinance.get_history("AAPL", period="5d", interval="1d")

    assert len(bars) == 2
    assert bars[0]["close"] == 101.0
    assert bars[0]["volume"] == 10000
    assert isinstance(bars[0]["time"], int)
    assert bars[1]["high"] == 103.0


@pytest.mark.asyncio
async def test_get_history_returns_empty_list_for_empty_frame():
    with patch.object(yfinance.yf, "Ticker", return_value=FakeTicker(history=pd.DataFrame())):
        bars = await yfinance.get_history("X")
    assert bars == []


# ── get_info ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_info_returns_info_dict():
    ticker = FakeTicker(info={"longName": "Apple Inc.", "trailingPE": 30.5})
    with patch.object(yfinance.yf, "Ticker", return_value=ticker):
        info = await yfinance.get_info("AAPL")
    assert info["longName"] == "Apple Inc."


@pytest.mark.asyncio
async def test_get_info_returns_empty_dict_when_info_is_none():
    ticker = FakeTicker(info=None)
    with patch.object(yfinance.yf, "Ticker", return_value=ticker):
        info = await yfinance.get_info("X")
    assert info == {}


# ── get_batch_quotes ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_batch_quotes_short_circuits_on_empty_list():
    out = await yfinance.get_batch_quotes([])
    assert out == {}


@pytest.mark.asyncio
async def test_get_batch_quotes_parses_multiindex_dataframe():
    """yf.download with multiple tickers returns a DataFrame with a
    MultiIndex on columns (level 0 = ticker, level 1 = OHLCV)."""
    cols = pd.MultiIndex.from_product(
        [["AAPL", "MSFT"], ["Open", "High", "Low", "Close", "Volume"]]
    )
    df = pd.DataFrame(
        [
            [200, 205, 199, 203, 1000, 410, 420, 408, 415, 2000],
            [201, 206, 200, 204, 1500, 412, 422, 411, 420, 2500],
        ],
        columns=cols,
    )

    with patch.object(yfinance.yf, "download", return_value=df):
        out = await yfinance.get_batch_quotes(["AAPL", "MSFT"])

    assert set(out.keys()) == {"AAPL", "MSFT"}
    assert out["AAPL"]["price"] == 204.0
    assert out["AAPL"]["change_pct"] == round((204 - 203) / 203 * 100, 4)
    assert out["AAPL"]["volume"] == 1500
    assert out["MSFT"]["price"] == 420.0


@pytest.mark.asyncio
async def test_get_batch_quotes_handles_single_symbol_flat_dataframe():
    """When yf.download is called with a single ticker it returns a
    flat (non-MultiIndex) DataFrame. The connector keys it under
    tickers[0]."""
    df = pd.DataFrame({
        "Open":   [200, 201],
        "High":   [205, 206],
        "Low":    [199, 200],
        "Close":  [203, 204],
        "Volume": [1000, 2000],
    })
    with patch.object(yfinance.yf, "download", return_value=df):
        out = await yfinance.get_batch_quotes(["AAPL"])
    assert "AAPL" in out
    assert out["AAPL"]["price"] == 204.0


@pytest.mark.asyncio
async def test_get_batch_quotes_returns_empty_dict_when_download_raises():
    with patch.object(yfinance.yf, "download", side_effect=RuntimeError("Yahoo blocked")):
        out = await yfinance.get_batch_quotes(["AAPL", "MSFT"])
    assert out == {}


# ── get_financials ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_financials_returns_three_lists_of_records():
    income = pd.DataFrame({"2024-09-30": [100.0], "2023-09-30": [90.0]}, index=["Revenue"])
    balance = pd.DataFrame({"2024-09-30": [500.0]}, index=["TotalAssets"])
    cash = pd.DataFrame({"2024-09-30": [50.0]}, index=["FreeCashFlow"])
    ticker = FakeTicker(financials=income, balance_sheet=balance, cashflow=cash)

    with patch.object(yfinance.yf, "Ticker", return_value=ticker):
        f = await yfinance.get_financials("AAPL")

    assert set(f.keys()) == {"income_statement", "balance_sheet", "cash_flow"}
    # `_df_to_list` transposes (.T) so each record is a row keyed by
    # the original index labels. Both periods (years) become records.
    assert len(f["income_statement"]) == 2
    # Records carry the metric values; first record's Revenue should
    # match what we put into the frame (the order after .T isn't
    # guaranteed across pandas versions — assert presence + values).
    revenues = sorted(rec["Revenue"] for rec in f["income_statement"])
    assert revenues == [90.0, 100.0]


@pytest.mark.asyncio
async def test_get_financials_returns_empty_lists_when_frames_empty():
    ticker = FakeTicker()  # all three default to empty DataFrames
    with patch.object(yfinance.yf, "Ticker", return_value=ticker):
        f = await yfinance.get_financials("X")
    assert f == {"income_statement": [], "balance_sheet": [], "cash_flow": []}
