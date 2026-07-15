from datetime import datetime, timedelta, timezone

from api.market_data_quality import build_quality_meta


def test_missing_quote_is_fail_closed_unavailable():
    meta = build_quality_meta(
        {"price": 0, "data_source": "unavailable"},
        kind="quote",
        fallback_chain=["primary", "fallback"],
    )
    assert meta.freshness == "unavailable"
    assert meta.fallback_chain == ["primary", "fallback"]


def test_explicit_stale_source_wins_even_with_values():
    meta = build_quality_meta(
        {"pe_ratio": 20, "data_source": "db_stale", "fetched_at": datetime.now(timezone.utc).isoformat()},
        kind="fundamentals",
        fallback_chain=["twse", "postgres_stale"],
    )
    assert meta.freshness == "stale"


def test_old_history_anchor_is_stale():
    old = datetime.now(timezone.utc) - timedelta(days=10)
    meta = build_quality_meta(
        {"close": 100}, kind="history", as_of=old.isoformat(), fallback_chain=["postgres"],
    )
    assert meta.freshness == "stale"
    assert meta.market_session == "historical"


def test_recent_quote_is_fresh_and_has_regular_session():
    meta = build_quality_meta(
        {"price": 100, "ts": int(datetime.now(timezone.utc).timestamp() * 1000), "is_market_open": True,
         "data_source": "primary"},
        kind="quote",
        fallback_chain=["primary"],
    )
    assert meta.freshness == "fresh"
    assert meta.market_session == "regular"
