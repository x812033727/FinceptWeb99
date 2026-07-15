from datetime import UTC, datetime, timedelta

import pytest

from services import quote_freshness_service as freshness


def test_parse_quote_time_accepts_provider_precision_and_iso():
    expected = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    seconds = expected.timestamp()
    assert freshness.parse_quote_time(seconds) == expected
    assert freshness.parse_quote_time(seconds * 1_000) == expected
    assert freshness.parse_quote_time(seconds * 1_000_000) == expected
    assert freshness.parse_quote_time(seconds * 1_000_000_000) == expected
    assert freshness.parse_quote_time("2026-07-15T12:00:00Z") == expected


@pytest.mark.parametrize("market,max_age", [("US", 90), ("TW", 90), ("CRYPTO", 30)])
def test_market_age_limits_are_inclusive(market, max_age):
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    source = {"US": "polygon", "TW": "twse_mis", "CRYPTO": "kraken"}[market]
    result = freshness.assess_quote_freshness(
        market,
        {
            "ts": (now - timedelta(seconds=max_age)).timestamp() * 1_000,
            "data_source": source,
        },
        now=now,
    )
    assert result.is_fresh is True
    assert result.reason == "fresh"
    assert result.age_seconds == pytest.approx(max_age)


@pytest.mark.parametrize(
    ("quote", "reason"),
    [
        ({}, "missing_source"),
        ({"data_source": "unavailable"}, "source_unavailable"),
        ({"ts": "not-a-time", "data_source": "polygon"}, "missing_timestamp"),
    ],
)
def test_unusable_quotes_return_explainable_reason(quote, reason):
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    result = freshness.assess_quote_freshness("US", quote, now=now)
    assert result.is_fresh is False
    assert result.reason == reason


def test_stale_future_and_non_executable_sources_are_rejected():
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    stale = freshness.assess_quote_freshness(
        "US",
        {
            "ts": (now - timedelta(seconds=91)).timestamp(),
            "data_source": "polygon",
        },
        now=now,
    )
    future = freshness.assess_quote_freshness(
        "CRYPTO",
        {
            "ts": (now + timedelta(seconds=31)).timestamp(),
            "data_source": "kraken",
        },
        now=now,
    )
    eod = freshness.assess_quote_freshness(
        "TW", {"ts": now.timestamp(), "data_source": "finmind"}, now=now
    )
    assert stale.reason == "stale_timestamp"
    assert future.reason == "future_timestamp"
    assert eod.reason == "non_executable_source"


def test_invalid_context_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        freshness.assess_quote_freshness("US", {"ts": 1}, now=datetime(2026, 7, 15))
    with pytest.raises(ValueError, match="unsupported market"):
        freshness.assess_quote_freshness(
            "XX",
            {"ts": 1, "data_source": "polygon"},
            now=datetime(2026, 7, 15, tzinfo=UTC),
        )
