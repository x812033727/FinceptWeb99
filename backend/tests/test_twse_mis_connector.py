"""Tests for `data.tw.twse_mis_connector`.

Covers the JSON envelope contract MIS publishes:
  - `rtcode != "0000"` → return None (symbol not found, rate-limited)
  - `msgArray` empty → return None
  - `z=="-"` AND `y=="-"` → return None (no trade ever, suspended)
  - Happy path: parse `z` / `o` / `h` / `l` / `v` / `y` / `n` / `tlong`
  - HTTP error (4xx / network) → return None (waterfall falls through)

Each test stubs `httpx.AsyncClient` so no real MIS calls are made.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from data.tw import twse_mis_connector as twse_mis


def _stub_response(*, status: int = 200, payload: dict | str | None = None):
    """Build a `httpx.Response`-shaped mock matching what the real
    client returns. `payload=str` simulates a non-JSON body so we can
    exercise the `ValueError` path."""
    resp = MagicMock()
    resp.status_code = status
    if isinstance(payload, str):
        resp.json = MagicMock(side_effect=ValueError("not json"))
    else:
        resp.json = MagicMock(return_value=payload or {})
    resp.raise_for_status = MagicMock()
    return resp


def _stub_client(response):
    """Replace `httpx.AsyncClient(...)` with an async context manager
    whose `.get(...)` returns `response`."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=response)
    return MagicMock(return_value=client)


@pytest.mark.asyncio
async def test_returns_normalized_quote_on_happy_path():
    """The connector must map MIS's field names (`z` / `y` / `o` / `h`
    / `l` / `v` / `n` / `tlong`) to the waterfall's expected shape
    (`close` / `prev_close` / `open` / `high` / `low` / `volume` /
    `name_zh`). A drift here would silently produce a quote dict with
    None close that downstream `_normalize_quote` would convert to a
    blank screener row."""
    payload = {
        "rtcode": "0000",
        "msgArray": [{
            "ch":    "2330.tw",
            "n":     "台積電",
            "z":     "1095.00",
            "y":     "1090.00",
            "o":     "1100.00",
            "h":     "1105.00",
            "l":     "1095.00",
            "v":     "12345600",
            "tv":    "1500",
            "tlong": "1716797400000",
            "ex":    "tse",
        }],
    }
    with patch.object(
        twse_mis.httpx, "AsyncClient",
        new=_stub_client(_stub_response(payload=payload)),
    ):
        out = await twse_mis.get_realtime_quote("2330")

    assert out is not None
    assert out["symbol"] == "2330"
    assert out["name_zh"] == "台積電"
    assert out["close"] == 1095.0
    assert out["prev_close"] == 1090.0
    assert out["open"] == 1100.0
    assert out["high"] == 1105.0
    assert out["low"] == 1095.0
    assert out["volume"] == 12345600
    assert out["tlong"] == "1716797400000"


@pytest.mark.asyncio
async def test_returns_none_when_rtcode_not_ok():
    """`rtcode="0001"` happens on symbol-not-found or temporary
    rate-limit. Must return None so the waterfall falls through to
    STOCK_DAY_ALL — NOT raise (the public-page endpoint is best-
    effort by design)."""
    payload = {"rtcode": "0001", "rtmessage": "no data", "msgArray": []}
    with patch.object(
        twse_mis.httpx, "AsyncClient",
        new=_stub_client(_stub_response(payload=payload)),
    ):
        out = await twse_mis.get_realtime_quote("9999")
    assert out is None


@pytest.mark.asyncio
async def test_returns_none_when_msg_array_empty():
    """`rtcode=0000` with empty msgArray = malformed ex_ch (wrong
    exchange prefix). Defensive: return None instead of indexing
    `arr[0]` and raising."""
    payload = {"rtcode": "0000", "msgArray": []}
    with patch.object(
        twse_mis.httpx, "AsyncClient",
        new=_stub_client(_stub_response(payload=payload)),
    ):
        out = await twse_mis.get_realtime_quote("2330")
    assert out is None


@pytest.mark.asyncio
async def test_returns_none_when_no_trade_and_no_prev_close():
    """`z="-"` AND `y="-"` = the symbol hasn't traded yet today AND
    there's no prior reference. The waterfall's STOCK_DAY_ALL tier
    has yesterday's close at minimum, so falling through is the
    right move."""
    payload = {
        "rtcode": "0000",
        "msgArray": [{
            "ch": "9999.tw", "n": "未上市", "z": "-", "y": "-",
            "o": "-", "h": "-", "l": "-", "v": "0", "tlong": "0",
        }],
    }
    with patch.object(
        twse_mis.httpx, "AsyncClient",
        new=_stub_client(_stub_response(payload=payload)),
    ):
        out = await twse_mis.get_realtime_quote("9999")
    assert out is None


@pytest.mark.asyncio
async def test_keeps_quote_when_only_prev_close_present():
    """Pre-open: TWSE intraday has no trades yet but `y` carries
    yesterday's close. Returning this still beats falling through to
    STOCK_DAY_ALL because the symbol's other fields (open / high /
    low) are at least zeroed consistently."""
    payload = {
        "rtcode": "0000",
        "msgArray": [{
            "ch": "2330.tw", "n": "台積電",
            "z": "-", "y": "1090.00",
            "o": "-", "h": "-", "l": "-", "v": "0", "tlong": "0",
        }],
    }
    with patch.object(
        twse_mis.httpx, "AsyncClient",
        new=_stub_client(_stub_response(payload=payload)),
    ):
        out = await twse_mis.get_realtime_quote("2330")
    assert out is not None
    assert out["close"] is None
    assert out["prev_close"] == 1090.0


@pytest.mark.asyncio
async def test_http_error_returns_none():
    """HTTP 403 from missing JSESSIONID cookie warm-up MUST NOT
    bubble — the waterfall's contract is "return None on failure".
    The next tier handles it."""
    with patch.object(
        twse_mis.httpx, "AsyncClient",
        new=_stub_client(_stub_response(status=403, payload={})),
    ):
        out = await twse_mis.get_realtime_quote("2330")
    assert out is None


@pytest.mark.asyncio
async def test_network_failure_returns_none():
    """A transient `httpx.ConnectError` (DNS fail / TLS hiccup) must
    not propagate. Same fail-closed semantics as the rest of the TW
    connector layer."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=httpx.ConnectError("dns boom"))
    with patch.object(
        twse_mis.httpx, "AsyncClient", new=MagicMock(return_value=client),
    ):
        out = await twse_mis.get_realtime_quote("2330")
    assert out is None


@pytest.mark.asyncio
async def test_non_json_body_returns_none():
    """MIS occasionally serves an HTML error page (e.g. site
    maintenance). The connector must tolerate `r.json()` raising
    ValueError and return None instead of crashing."""
    with patch.object(
        twse_mis.httpx, "AsyncClient",
        new=_stub_client(_stub_response(payload="<html>Maintenance</html>")),
    ):
        out = await twse_mis.get_realtime_quote("2330")
    assert out is None


@pytest.mark.asyncio
async def test_uses_otc_prefix_for_tpex_listed():
    """TPEx symbols (上櫃) MUST use the `otc_` prefix in `ex_ch` —
    feeding `tse_5483.tw` (when 5483 is a 上櫃 stock) returns
    rtcode=0001. The connector reads the explicit `exchange` arg
    rather than hard-coding `tse_`."""
    captured: dict = {}

    async def _fake_get(url, params=None, headers=None):
        captured["params"] = params
        return _stub_response(payload={
            "rtcode": "0000",
            "msgArray": [{
                "ch": "5483.tw", "n": "中美晶",
                "z": "150.00", "y": "148.00", "o": "149", "h": "150",
                "l": "148", "v": "100000", "tlong": "1716000000000",
            }],
        })

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=_fake_get)
    with patch.object(
        twse_mis.httpx, "AsyncClient", new=MagicMock(return_value=client),
    ):
        await twse_mis.get_realtime_quote("5483", exchange="TPEx")

    assert captured["params"]["ex_ch"].startswith("otc_"), captured["params"]
    assert captured["params"]["ex_ch"] == "otc_5483.tw"


@pytest.mark.asyncio
async def test_default_exchange_uses_tse_prefix():
    """`exchange="TWSE"` (the default) maps to `tse_` — confirms the
    fallback used by code paths that don't know a symbol's exchange
    is the conservative (more common) TWSE-listed prefix."""
    captured: dict = {}

    async def _fake_get(url, params=None, headers=None):
        captured["params"] = params
        return _stub_response(payload={
            "rtcode": "0000",
            "msgArray": [{
                "ch": "2330.tw", "n": "台積電",
                "z": "1095", "y": "1090", "o": "1100", "h": "1105",
                "l": "1095", "v": "100", "tlong": "1716000000000",
            }],
        })

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=_fake_get)
    with patch.object(
        twse_mis.httpx, "AsyncClient", new=MagicMock(return_value=client),
    ):
        await twse_mis.get_realtime_quote("2330")

    assert captured["params"]["ex_ch"] == "tse_2330.tw"
    # Sanity: the cache-buster `_` param must be present so MIS
    # doesn't serve us a cached JSON from its CDN tier.
    assert "_" in captured["params"]


# Smoke-check that the raw JSON envelope structure the connector
# expects matches the documented MIS response. Pinned to a real
# captured envelope so a future MIS schema change (e.g. nested
# msgArray, renamed `z` → `lastPrice`) breaks loudly here instead of
# silently falling through to STOCK_DAY_ALL.
_REAL_CAPTURED_ENVELOPE = {
    "msgArray": [{
        "tk1": "2330.tw_TSE_0_0_0",
        "tk0": "2330.tw_TSE_0_0_0",
        "oa":  "1100.00",
        "ob":  "1099.00",
        "tlong":  "1716797400000",
        "ot":  "13:30:00",
        "f":   "100",
        "ex":  "tse",
        "g":   "200",
        "ov":  "300",
        "d":   "20260527",
        "it":  "12",
        "b":   "1094.00_1093.00_1092.00_1091.00_1090.00",
        "c":   "2330",
        "mt":  "100",
        "a":   "1096.00_1097.00_1098.00_1099.00_1100.00",
        "n":   "台積電",
        "o":   "1100.00",
        "l":   "1095.00",
        "oz":  "150",
        "h":   "1105.00",
        "ip":  "1",
        "i":   "8",
        "w":   "990.00",
        "nf":  "台灣積體電路製造股份有限公司",
        "ch":  "2330.tw",
        "zg":  "100",
        "tv":  "1500",
        "z":   "1095.00",
        "ts":  "0",
        "u":   "1199.00",
        "y":   "1090.00",
        "v":   "12345600",
        "fv":  "12345600",
        "key": "0.6928_tse_2330.tw_",
        "pid": "0",
        "ps":  "12345600",
    }],
    "referer": "",
    "userDelay": 5000,
    "rtcode": "0000",
    "queryTime": {
        "sysDate": "20260527",
        "stockInfoItem": 1,
        "stockInfo": 1,
        "sessionStr": "UserSession",
        "sysTime": "13:30:00",
        "showChart": False,
        "sessionFromTime": -1,
        "sessionLatestTime": -1,
    },
    "rtmessage": "OK",
}


@pytest.mark.asyncio
async def test_real_envelope_parses_cleanly():
    """Pin against a real captured MIS envelope (2330, 2026-05-27).
    Failure here = the MIS API changed shape; the waterfall would
    silently fall through to STOCK_DAY_ALL and the user would see
    yesterday's close instead of today's intraday tick.
    """
    # Round-trip through JSON to mimic the network path exactly.
    payload = json.loads(json.dumps(_REAL_CAPTURED_ENVELOPE))
    with patch.object(
        twse_mis.httpx, "AsyncClient",
        new=_stub_client(_stub_response(payload=payload)),
    ):
        out = await twse_mis.get_realtime_quote("2330")
    assert out is not None
    assert out["close"] == 1095.0
    assert out["volume"] == 12345600
    assert out["prev_close"] == 1090.0
