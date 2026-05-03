"""Unit tests for `services.event_calendar_service`.

Mocks the yfinance connector + Redis cache so the suite stays
offline. Covers the date-coercion logic, lookahead window filtering,
next-event picking, and the cache-hit / cache-miss paths.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

# Stub `data.us.yfinance_connector` so the lazy import inside the
# service resolves without pulling in pandas / yfinance (heavy
# transitive deps that may not be installed in unit-test envs).
# Tests patch `get_calendar` on this stub directly. Production code
# imports the real module — sys.modules registration only takes
# effect for THIS process while tests run.
if "data.us.yfinance_connector" not in sys.modules:
    _stub = types.ModuleType("data.us.yfinance_connector")
    _stub.get_calendar = AsyncMock(return_value=None)
    sys.modules["data.us.yfinance_connector"] = _stub
    _data_us = sys.modules.setdefault(
        "data.us", types.ModuleType("data.us"),
    )
    _data_us.yfinance_connector = _stub

from services import event_calendar_service as svc  # noqa: E402


# ── _suffix_for_market ────────────────────────────────────────────


def test_tw_symbol_gets_dot_tw_suffix():
    assert svc._suffix_for_market("TW", "2330") == "2330.TW"


def test_tw_symbol_already_suffixed_passes_through():
    """Don't double-suffix when caller pre-mapped (e.g. 上櫃 .TWO)."""
    assert svc._suffix_for_market("TW", "2330.TW") == "2330.TW"
    assert svc._suffix_for_market("TW", "6488.TWO") == "6488.TWO"


def test_us_symbol_passes_through_unchanged():
    assert svc._suffix_for_market("US", "AAPL") == "AAPL"


# ── _days_until ───────────────────────────────────────────────────


def test_days_until_future_date_returns_positive():
    assert svc._days_until("2026-05-12", date(2026, 5, 3)) == 9


def test_days_until_today_returns_zero():
    assert svc._days_until("2026-05-03", date(2026, 5, 3)) == 0


def test_days_until_past_date_returns_negative():
    assert svc._days_until("2026-04-30", date(2026, 5, 3)) == -3


def test_days_until_none_or_unparseable_returns_none():
    assert svc._days_until(None, date(2026, 5, 3)) is None
    assert svc._days_until("not-a-date", date(2026, 5, 3)) is None


def test_days_until_truncates_iso_with_time_suffix():
    """Yahoo sometimes returns datetimes; helper takes the first 10
    chars so a `2026-05-12T00:00:00` still parses cleanly."""
    assert svc._days_until("2026-05-12T09:00:00", date(2026, 5, 3)) == 9


# ── get_upcoming_event end-to-end ─────────────────────────────────


def _patches(*, cached=None, calendar):
    return (
        patch("services.event_calendar_service.cache_get",
              new=AsyncMock(return_value=cached)),
        patch("services.event_calendar_service.cache_set", new=AsyncMock()),
        patch("data.us.yfinance_connector.get_calendar",
              new=AsyncMock(return_value=calendar)),
    )


@pytest.mark.asyncio
async def test_returns_none_when_yfinance_fails():
    """yfinance raise → log + return None, don't propagate."""
    with patch(
        "services.event_calendar_service.cache_get",
        new=AsyncMock(return_value=None),
    ), patch(
        "data.us.yfinance_connector.get_calendar",
        new=AsyncMock(side_effect=RuntimeError("yahoo down")),
    ):
        result = await svc.get_upcoming_event(
            "TW", "2330", as_of=date(2026, 5, 3),
        )
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_no_events_in_lookahead_window():
    """Yahoo returns dates but they're > 14 days out → block reads
    `upcoming_event=None`, persona treats as 'no scheduled event'.
    The empty result MUST also be cached so we don't keep re-fetching
    Yahoo for symbols with no near-term calendar coverage."""
    cache_set_mock = AsyncMock()
    with patch(
        "services.event_calendar_service.cache_get",
        new=AsyncMock(return_value=None),
    ), patch(
        "services.event_calendar_service.cache_set", new=cache_set_mock,
    ), patch(
        "data.us.yfinance_connector.get_calendar",
        new=AsyncMock(return_value={
            "earnings_date":    "2026-08-01",   # > 14 days
            "ex_dividend_date": "2026-09-15",
        }),
    ):
        result = await svc.get_upcoming_event(
            "TW", "2330", as_of=date(2026, 5, 3),
        )
    assert result is None
    # Sentinel cached so next call within TTL doesn't re-hit Yahoo.
    cache_set_mock.assert_awaited_once()
    args, _ = cache_set_mock.call_args
    assert json.loads(args[1]) == "_no_event"


@pytest.mark.asyncio
async def test_picks_earnings_when_sooner_than_ex_dividend():
    p1, p2, p3 = _patches(calendar={
        "earnings_date":    "2026-05-12",   # in 9 days
        "ex_dividend_date": "2026-05-15",   # in 12 days
    })
    with p1, p2, p3:
        result = await svc.get_upcoming_event(
            "TW", "2330", as_of=date(2026, 5, 3),
        )
    assert result is not None
    assert result["earnings_date"] == "2026-05-12"
    assert result["earnings_in_days"] == 9
    assert result["ex_dividend_date"] == "2026-05-15"
    assert result["ex_dividend_in_days"] == 12
    assert result["next_event"] == "earnings"
    assert result["next_event_in_days"] == 9


@pytest.mark.asyncio
async def test_picks_ex_dividend_when_sooner_than_earnings():
    p1, p2, p3 = _patches(calendar={
        "earnings_date":    "2026-05-15",   # in 12 days
        "ex_dividend_date": "2026-05-08",   # in 5 days
    })
    with p1, p2, p3:
        result = await svc.get_upcoming_event(
            "TW", "2330", as_of=date(2026, 5, 3),
        )
    assert result is not None
    assert result["next_event"] == "ex_dividend"
    assert result["next_event_in_days"] == 5


@pytest.mark.asyncio
async def test_filters_out_past_events_returns_none_when_only_past():
    """Earnings was last week + no upcoming dividend → all in-window
    fields None → service returns None entirely (not a half-empty
    dict)."""
    p1, p2, p3 = _patches(calendar={
        "earnings_date":    "2026-04-25",   # in -8 days
        "ex_dividend_date": None,
    })
    with p1, p2, p3:
        result = await svc.get_upcoming_event(
            "TW", "2330", as_of=date(2026, 5, 3),
        )
    assert result is None


@pytest.mark.asyncio
async def test_only_one_event_type_present():
    """Yahoo carries earnings but no dividend → return only earnings
    fields; ex_dividend fields stay None; next_event = earnings."""
    p1, p2, p3 = _patches(calendar={
        "earnings_date":    "2026-05-10",
        "ex_dividend_date": None,
    })
    with p1, p2, p3:
        result = await svc.get_upcoming_event(
            "TW", "2330", as_of=date(2026, 5, 3),
        )
    assert result is not None
    assert result["earnings_in_days"] == 7
    assert result["ex_dividend_date"] is None
    assert result["ex_dividend_in_days"] is None
    assert result["next_event"] == "earnings"


@pytest.mark.asyncio
async def test_cache_hit_returns_cached_payload_without_fetch():
    cached_payload = {
        "as_of": "2026-05-03", "earnings_date": "2026-05-12",
        "earnings_in_days": 9, "ex_dividend_date": None,
        "ex_dividend_in_days": None, "next_event": "earnings",
        "next_event_in_days": 9,
    }
    yf_mock = AsyncMock()
    with patch(
        "services.event_calendar_service.cache_get",
        new=AsyncMock(return_value=json.dumps(cached_payload)),
    ), patch("data.us.yfinance_connector.get_calendar", new=yf_mock):
        result = await svc.get_upcoming_event(
            "TW", "2330", as_of=date(2026, 5, 3),
        )
    assert result == cached_payload
    yf_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_hit_no_event_sentinel_returns_none():
    """The sentinel `"_no_event"` cached payload must round-trip back
    to None (the public API contract for "no events in window")."""
    yf_mock = AsyncMock()
    with patch(
        "services.event_calendar_service.cache_get",
        new=AsyncMock(return_value=json.dumps("_no_event")),
    ), patch("data.us.yfinance_connector.get_calendar", new=yf_mock):
        result = await svc.get_upcoming_event(
            "TW", "2330", as_of=date(2026, 5, 3),
        )
    assert result is None
    yf_mock.assert_not_awaited()
