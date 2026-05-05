"""Static-shape tests for `data.tw.finmind_datasets`.

The registry is data, not behaviour, so the tests just guard against:
  - duplicate dataset names within a category or across the registry,
  - trivially empty / mistyped specs,
  - the typed connector wrappers calling datasets that aren't in the
    registry (which would mean the registry has gone stale).

When FinMind adds or renames a dataset, update `finmind_datasets.py`
+ rerun this suite — failures here are loud documentation, not
breakage.
"""
from __future__ import annotations

from data.tw import finmind_datasets as fd


# ── Shape ─────────────────────────────────────────────────────────


def test_registry_categories_match_finmind_doc_layout():
    """The seven category buckets mirror the FinMind DataList page —
    if a new category appears in the docs, surface it explicitly here
    rather than dropping new datasets into 'other'."""
    assert set(fd.REGISTRY) == {
        "technical", "chip", "fundamental", "derivative",
        "realtime", "convertible_bond", "other",
    }


def test_every_category_has_at_least_one_dataset():
    """Empty category = wiring mistake, not 'we deliberately removed
    every realtime dataset'."""
    for category, specs in fd.REGISTRY.items():
        assert len(specs) > 0, f"category {category!r} is empty"


def test_dataset_specs_have_non_empty_name_and_description():
    """Empty `name` would silently 200-empty against FinMind. Empty
    `description` defeats the registry's documentation purpose."""
    for spec in fd.all_datasets():
        assert spec.name.strip() != "", "empty dataset name"
        assert spec.description.strip() != "", (
            f"{spec.name} has no description"
        )


def test_no_duplicate_dataset_names_across_registry():
    """Same FinMind name in two categories = future caller can't tell
    which entry to use. Catch at registry-build time."""
    seen: set[str] = set()
    dups: list[str] = []
    for spec in fd.all_datasets():
        if spec.name in seen:
            dups.append(spec.name)
        seen.add(spec.name)
    assert dups == [], f"duplicate dataset names: {dups}"


# ── find_dataset() ────────────────────────────────────────────────


def test_find_dataset_resolves_known_name():
    spec = fd.find_dataset("TaiwanStockPER")
    assert spec is not None
    assert spec.per_symbol is True
    assert "PER" in spec.description or "PE" in spec.description


def test_find_dataset_returns_none_for_unknown():
    assert fd.find_dataset("NotARealFinmindDataset") is None


def test_find_dataset_is_case_sensitive():
    """FinMind dataset names are case-sensitive at the API; the
    resolver must be too — otherwise a typo'd lookup silently picks
    up the wrong endpoint at the API layer."""
    assert fd.find_dataset("taiwanstockper") is None
    assert fd.find_dataset("TaiwanStockPER") is not None


# ── Cross-check: typed wrappers reference registered datasets ─────


def test_connector_typed_wrappers_use_registered_datasets():
    """Every dataset name hard-coded in a `finmind_connector.get_X`
    typed wrapper must exist in the registry. Stops the connector and
    the registry from drifting apart silently."""
    expected = {
        "TaiwanStockPrice",
        "TaiwanStockInstitutionalInvestors",  # legacy alias used by get_institutional
        "TaiwanStockMarginPurchaseShortSale",
        "TaiwanStockMonthRevenue",
        "TaiwanStockFinancialStatements",
        "TaiwanStockBalanceSheet",
        "TaiwanStockCashFlowsStatement",
        "TaiwanStockDividend",
        "TaiwanStockHoldingSharesPer",
        "TaiwanStockShareholding",
        "TaiwanStockTotalInstitutionalInvestors",
        "TaiwanStockDispositionSecuritiesPeriod",
        "TaiwanStockSuspended",
        "TaiwanStockDayTrading",
        "TaiwanStockTotalReturnIndex",
        "TaiwanStockBuyBack",
        "TaiwanStockNews",
        "TaiwanStockPER",
        "TaiwanStockSecuritiesLending",
        "TaiwanStockPriceAdj",
        "TaiwanStockTotalMarginPurchaseShortSale",
        "TaiwanTotalExchangeMarginMaintenance",
        "TaiwanStockBlockTrade",
        "TaiwanFuturesInstitutionalInvestors",
        "TaiwanFuturesInstitutionalInvestorsAfterHours",
        "TaiwanOptionInstitutionalInvestors",
        "TaiwanBusinessIndicator",
        # `TaiwanStockGovernmentBankBuySell` (capital S in `Stock`) is
        # what FinMind's API actually accepts; the previous catalog
        # entry had a typo'd lowercase 's' that 422'd at the FinMind
        # endpoint validator before any tier/quota check.
        "TaiwanStockGovernmentBankBuySell",
    }
    registered_names = {spec.name for spec in fd.all_datasets()}
    missing = expected - registered_names
    # `TaiwanStockInstitutionalInvestors` is the LEGACY name; the
    # documented one is `TaiwanStockInstitutionalInvestorsBuySell`.
    # Connector keeps the legacy alias for backward compatibility,
    # so we accept either being registered.
    if (
        "TaiwanStockInstitutionalInvestors" in missing
        and "TaiwanStockInstitutionalInvestorsBuySell" in registered_names
    ):
        missing.discard("TaiwanStockInstitutionalInvestors")
    assert missing == set(), (
        f"connector references datasets missing from registry: {missing}"
    )
