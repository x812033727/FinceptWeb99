"""Pin the `single_day` flag on the three TAIFEX dealer/spread datasets.

FinMind silently returns ONLY start_date's rows on a multi-day range
query for these datasets (verified live 2026-07-28: a range request for
2026-07-22..2026-07-27 returned only 2026-07-22's rows, while a
single-day request for 2026-07-24 returned that day correctly). With
the flag unset the incremental ingest window never advanced past the
last ingested date — tables froze at 2026-07-21 while dataset_sources
kept marking them fresh. `single_day=True` makes the runner fan out
day-by-day (omitting end_date), which is the only mode FinMind honours
here. These tests keep the flag from regressing in both the mapping
registry and the dataset catalog.
"""
from __future__ import annotations

import pytest

from finmind.dataset_catalog import all_entries
from finmind.ingest.mappings import MAPPINGS

TAIFEX_SINGLE_DAY_DATASETS = (
    "TaiwanFuturesDealerTradingVolumeDaily",
    "TaiwanOptionDealerTradingVolumeDaily",
    "TaiwanFuturesSpreadTrading",
)


@pytest.mark.parametrize("dataset_code", TAIFEX_SINGLE_DAY_DATASETS)
def test_mapping_is_single_day(dataset_code: str) -> None:
    mapping = MAPPINGS[dataset_code]
    assert mapping.single_day is True, (
        f"{dataset_code} must be single_day: FinMind returns only "
        "start_date's rows on range queries for this dataset"
    )


@pytest.mark.parametrize("dataset_code", TAIFEX_SINGLE_DAY_DATASETS)
def test_catalog_is_single_day(dataset_code: str) -> None:
    entries = [e for _, e in all_entries() if e.dataset_code == dataset_code]
    assert entries, f"{dataset_code} missing from dataset catalog"
    for entry in entries:
        assert entry.single_day is True, (
            f"catalog entry for {dataset_code} must mirror the "
            "single_day=True mapping flag"
        )
