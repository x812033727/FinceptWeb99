"""Catalog lookup used by the runner's 4xx fallback routing."""
from finmind.dataset_catalog import fallback_source_for


def test_buyback_falls_back_to_twse():
    # The proving case: TaiwanStockBuyBack has 422'd daily on FinMind
    # while a registered TWSE self-crawl fetcher sat unused.
    assert fallback_source_for("TaiwanStockBuyBack") == "twse"


def test_unknown_dataset_has_no_fallback():
    assert fallback_source_for("NoSuchDataset") is None


def test_dataset_without_fallback_returns_none():
    # Crypto datasets are FinMind/binance-native; entries whose
    # fallback_source is None must not fabricate one.
    from finmind.dataset_catalog import all_entries
    no_fb = next(
        (e.dataset_code for _, e in all_entries() if e.fallback_source is None),
        None,
    )
    if no_fb is not None:
        assert fallback_source_for(no_fb) is None
