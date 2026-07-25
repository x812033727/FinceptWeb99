"""Content-level freshness: read max(ts) per dataset table instead of
trusting job health records, which have reported `failed` on runs that
wrote 15k rows and `ok` on runs that wrote none."""
from datetime import date

from tasks.monitor_archive_freshness import stale_datasets


def test_fresh_archive_reports_nothing():
    latest = {
        "ohlcv_tw": date(2026, 7, 24),
        "taiex": date(2026, 7, 24),
        "institutional_tw": date(2026, 7, 24),
        "margin_tw": date(2026, 7, 24),
    }
    assert stale_datasets(latest, expected=date(2026, 7, 24)) == []


def test_lagging_dataset_is_named_with_its_lag():
    latest = {
        "ohlcv_tw": date(2026, 7, 24),
        "taiex": date(2026, 7, 24),
        "institutional_tw": date(2026, 7, 21),   # 3 days behind
        "margin_tw": date(2026, 7, 24),
    }
    stale = stale_datasets(latest, expected=date(2026, 7, 24))
    assert stale == ["institutional_tw: 2026-07-21 (expected 2026-07-24)"]


def test_empty_table_is_stale_not_a_crash():
    latest = {"ohlcv_tw": None}
    stale = stale_datasets(latest, expected=date(2026, 7, 24))
    assert stale == ["ohlcv_tw: empty (expected 2026-07-24)"]
