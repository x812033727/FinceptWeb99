"""
Unit tests for data.us.stooq_connector.

Covers the regression that prompted PR #34: a single-symbol GET with
the original `f=sd2t2ohlcvp` field set could come back HTTP 200 with
the body `DNS cache overflow`, which `csv.DictReader` silently parsed
as a zero-row header. We now drop d2t2 and detect non-CSV bodies
explicitly. Tests assert both: (a) the new body-shape guard, (b) the
parsing path for valid responses.

httpx.AsyncClient is mocked at the import-site (the connector creates
clients inline via `async with httpx.AsyncClient(...) as c:`) so no
network is touched.
"""
import asyncio
from unittest.mock import patch

import pytest

import data.us.stooq_connector as stooq


# ── Test doubles ──────────────────────────────────────────────────

class FakeResponse:
    """Mimics the subset of httpx.Response the connector touches."""
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class FakeClient:
    """Mock for `httpx.AsyncClient` used as an async-context manager.

    Caller passes a `responder` that maps Stooq's `s=<symbol>.us` query
    param to a FakeResponse, so each test case can shape the result
    per-symbol.
    """
    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, url: str, params: dict | None = None):
        params = params or {}
        self.calls.append((url, params))
        return self.responder(params.get("s", ""))


def install_client(responder):
    """Patch httpx.AsyncClient to yield a FakeClient. Returns the
    FakeClient instance so callers can inspect .calls afterwards."""
    fake = FakeClient(responder)
    return patch.object(stooq.httpx, "AsyncClient", lambda **_: fake), fake


# ── Pure helper tests ─────────────────────────────────────────────

def test_to_stooq_lowercases_and_appends_us():
    assert stooq._to_stooq("AAPL") == "aapl.us"
    assert stooq._to_stooq("BRK-B") == "brk-b.us"  # dash preserved
    assert stooq._to_stooq("msft") == "msft.us"


@pytest.mark.parametrize("raw,expected", [
    ("123.45", 123.45),
    ("0", 0.0),
    ("-1.5", -1.5),
    ("", None),
    ("N/D", None),
    (None, None),
    ("abc", None),
])
def test_safe_float(raw, expected):
    assert stooq._safe_float(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("12345", 12345),
    ("1.0", 1),
    ("", None),
    ("N/D", None),
])
def test_safe_int(raw, expected):
    assert stooq._safe_int(raw) == expected


# ── _row_to_quote ─────────────────────────────────────────────────

def test_row_to_quote_parses_full_row_and_computes_change():
    row = {
        "Symbol": "AAPL.US", "Open": "200", "High": "205",
        "Low": "199", "Close": "204", "Volume": "1234567", "Prev": "200",
    }
    q = stooq._row_to_quote(row)
    assert q == {
        "price": 204.0,
        "change": 4.0,
        "change_pct": 2.0,
        "volume": 1234567,
        "open": 200.0,
        "high": 205.0,
        "low": 199.0,
        "prev_close": 200.0,
    }


def test_row_to_quote_handles_missing_prev_with_zero_change():
    row = {"Close": "204", "Volume": "1000"}
    q = stooq._row_to_quote(row)
    assert q is not None
    assert q["price"] == 204.0
    assert q["change"] == 0
    assert q["change_pct"] == 0
    assert q["prev_close"] is None


def test_row_to_quote_returns_none_on_missing_close():
    assert stooq._row_to_quote({"Close": "", "Open": "1"}) is None
    assert stooq._row_to_quote({"Close": "N/D"}) is None
    assert stooq._row_to_quote({}) is None


# ── _fetch_one anti-scrape guard ──────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_one_returns_none_on_dns_cache_overflow_body(caplog):
    """The bug from #34: HTTP 200 with body `DNS cache overflow` must
    not be parsed as a CSV. Connector should log + return None."""
    def responder(_):
        return FakeResponse("DNS cache overflow")

    patcher, fake = install_client(responder)
    with patcher, caplog.at_level("WARNING"):
        async with stooq.httpx.AsyncClient() as c:
            row = await stooq._fetch_one(c, "AAPL")

    assert row is None
    assert any("non_csv_response" in rec.message for rec in caplog.records)
    assert fake.calls and fake.calls[0][1]["s"] == "aapl.us"


@pytest.mark.asyncio
async def test_fetch_one_uses_the_shorter_field_set_without_d2t2():
    """PR #34: f=sohlcvp avoids the anti-scrape; verify the connector
    is actually sending that field set."""
    def responder(_):
        return FakeResponse(
            "Symbol,Open,High,Low,Close,Volume,Prev\n"
            "AAPL.US,200,205,199,204,1000,200\n"
        )

    patcher, fake = install_client(responder)
    with patcher:
        async with stooq.httpx.AsyncClient() as c:
            await stooq._fetch_one(c, "AAPL")

    assert fake.calls[0][1]["f"] == "sohlcvp"


@pytest.mark.asyncio
async def test_fetch_one_returns_none_on_http_error():
    def responder(_):
        return FakeResponse("Symbol,Close\n", status_code=503)

    patcher, _ = install_client(responder)
    with patcher:
        async with stooq.httpx.AsyncClient() as c:
            row = await stooq._fetch_one(c, "AAPL")
    assert row is None


# ── get_quote ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_quote_returns_full_dict_on_success():
    def responder(_):
        return FakeResponse(
            "Symbol,Open,High,Low,Close,Volume,Prev\n"
            "AAPL.US,200,205,199,204,1000,200\n"
        )
    patcher, _ = install_client(responder)
    with patcher:
        q = await stooq.get_quote("AAPL")
    assert q["symbol"] == "AAPL"
    assert q["price"] == 204.0
    assert q["change_pct"] == 2.0
    assert q["volume"] == 1000
    assert q["market_cap"] is None  # Stooq doesn't expose this


@pytest.mark.asyncio
async def test_get_quote_returns_empty_dict_on_anti_scrape():
    def responder(_):
        return FakeResponse("DNS cache overflow")
    patcher, _ = install_client(responder)
    with patcher:
        q = await stooq.get_quote("AAPL")
    assert q == {}


@pytest.mark.asyncio
async def test_get_quote_returns_empty_dict_when_close_missing():
    def responder(_):
        return FakeResponse("Symbol,Open,High,Low,Close,Volume,Prev\nAAPL.US,200,205,199,,1000,200\n")
    patcher, _ = install_client(responder)
    with patcher:
        q = await stooq.get_quote("AAPL")
    assert q == {}


# ── get_batch_quotes ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_batch_quotes_short_circuits_on_empty_list():
    out = await stooq.get_batch_quotes([])
    assert out == {}


@pytest.mark.asyncio
async def test_get_batch_quotes_walks_list_skipping_failures():
    """Sequential walk: AAPL succeeds, MSFT fails (DNS overflow), NVDA
    succeeds. Result has only the successful symbols, keyed by
    *upper-case input* not Stooq's `.us` lower-case form."""
    table = {
        "aapl.us": "Symbol,Open,High,Low,Close,Volume,Prev\nAAPL.US,200,205,199,204,1000,200\n",
        "msft.us": "DNS cache overflow",
        "nvda.us": "Symbol,Open,High,Low,Close,Volume,Prev\nNVDA.US,100,110,98,108,5000,100\n",
    }
    def responder(s):
        return FakeResponse(table[s])

    patcher, _ = install_client(responder)
    # Skip the 0.2s inter-request sleep so the test runs instantly.
    # Use a no-op coroutine — `asyncio.sleep(0)` here would recurse
    # since asyncio.sleep is the thing being patched.
    async def _noop_sleep(*_a, **_k):
        return None

    with patcher, patch.object(asyncio, "sleep", new=_noop_sleep):
        out = await stooq.get_batch_quotes(["AAPL", "MSFT", "NVDA"])

    assert set(out.keys()) == {"AAPL", "NVDA"}
    assert out["AAPL"] == {"price": 204.0, "change_pct": 2.0, "volume": 1000}
    assert out["NVDA"] == {"price": 108.0, "change_pct": 8.0, "volume": 5000}


@pytest.mark.asyncio
async def test_get_batch_quotes_spaces_requests_with_inter_request_delay():
    """asyncio.sleep is called between every pair of fetches but not
    before the first one — N symbols → N-1 sleeps."""
    def responder(_):
        return FakeResponse(
            "Symbol,Open,High,Low,Close,Volume,Prev\nX.US,1,1,1,1,1,1\n"
        )
    patcher, _ = install_client(responder)
    sleep_calls: list[float] = []

    async def fake_sleep(secs):
        sleep_calls.append(secs)

    with patcher, patch.object(asyncio, "sleep", new=fake_sleep):
        await stooq.get_batch_quotes(["A", "B", "C", "D"])

    assert sleep_calls == [stooq._INTER_REQUEST_DELAY_S] * 3
