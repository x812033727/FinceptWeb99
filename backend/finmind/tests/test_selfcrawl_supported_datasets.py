"""Tests for `selfcrawl.supported_datasets` — per-dataset coverage map.

Covers what `is_source_implemented` doesn't: a source can have a
working connector overall (`twse` covers 6 datasets today) but not
yet handle a specific dataset (e.g. `TaiwanFuturesDaily`). The
AdminPage flip gatekeeper relies on this finer granularity to refuse
flips that would break the next ingest cycle.
"""
from __future__ import annotations

import pytest

from finmind.ingest.selfcrawl import (
    _SUPPORTED_DATASETS_PROVIDERS,
    covers_dataset,
    known_sources,
    register_supported_datasets,
    supported_datasets,
)


def test_known_sources_covers_registry_plus_finmind():
    """The flip-endpoint allowlist is derived from this. It must include
    finmind + every registered connector — crucially fred/binance/
    coingecko, which the old hardcoded literal omitted (blocking macro +
    crypto cutover)."""
    ks = known_sources()
    assert "finmind" in ks
    for src in ("twse", "tpex", "taifex", "mops", "tdcc", "fred",
                "binance", "coingecko"):
        assert src in ks, f"{src} missing from known_sources()"


def test_finmind_covers_every_catalog_dataset():
    """FinMind is the upstream paid source — by construction it must
    serve every dataset_code the catalog knows about. If this drifts,
    something is broken in `dataset_catalog.py`."""
    from finmind.dataset_catalog import all_entries

    fm = supported_datasets("finmind")
    catalog_codes = {entry.dataset_code for _, entry in all_entries()}
    assert fm == catalog_codes
    # Sanity: catalog isn't empty.
    assert len(fm) >= 60


def test_twse_covers_phase_a_to_b_ready_datasets():
    """The 6 wired TWSE handlers from `selfcrawl/twse.py:_DISPATCH`.
    Pin the set so accidentally dropping a handler shows up red."""
    twse = supported_datasets("twse")
    # Headline handlers required for the discussion ctx + main app.
    assert "TaiwanStockPrice" in twse
    assert "TaiwanStockInstitutionalInvestorsBuySell" in twse
    assert "TaiwanStockMarginPurchaseShortSale" in twse
    assert "TaiwanStockInfo" in twse
    assert "TaiwanStockPER" in twse
    assert "TaiwanStockTotalReturnIndex" in twse


def test_mops_covers_monthly_revenue_and_quarterly_statements():
    """MOPS connector covers monthly revenue + the 3 quarterly
    statements (Phase 2A). Pin the exact set so dropping any of these
    handlers shows up red."""
    mops = supported_datasets("mops")
    assert mops == {
        "TaiwanStockMonthRevenue",
        "TaiwanStockFinancialStatements",
        "TaiwanStockBalanceSheet",
        "TaiwanStockCashFlowsStatement",
    }


def test_stub_sources_return_empty_set():
    """tpex still routes to `_NotWiredYetClient` — no handlers, so
    `supported_datasets` must report empty so the gatekeeper rejects
    every flip targeted at it. (taifex wired in W2, tdcc in W3.)"""
    assert supported_datasets("tpex") == set()


def test_taifex_reports_daily_market_datasets():
    """W2 wired the TAIFEX daily futures/options reports; W4b added
    三大法人 futures. The gatekeeper must accept flips for exactly those."""
    assert supported_datasets("taifex") == {
        "TaiwanFuturesDaily",
        "TaiwanOptionDaily",
        "TaiwanFuturesInstitutionalInvestors",
    }


def test_tdcc_reports_holding_shares_per():
    """W3 wired the TDCC 股權分散 report."""
    assert supported_datasets("tdcc") == {"TaiwanStockHoldingSharesPer"}


def test_unknown_source_returns_empty_set_not_error():
    """The gatekeeper queries `supported_datasets(user_input)` — an
    unknown string should report 'no coverage', not raise. KeyError
    is reserved for `resolve_client` (which IS the right place to
    surface the bad config)."""
    assert supported_datasets("not_a_real_source") == set()


def test_covers_dataset_helper_matches_set_membership():
    """Convenience predicate must agree with the underlying set."""
    assert covers_dataset("twse", "TaiwanStockPrice") is True
    assert covers_dataset("twse", "TaiwanFuturesDaily") is False
    assert covers_dataset("tpex", "TaiwanStockPrice") is False
    assert covers_dataset("finmind", "TaiwanStockPrice") is True


def test_provider_exception_falls_back_to_empty_set(caplog):
    """A misconfigured provider mustn't break the AdminPage. Force
    one to raise, confirm we get an empty set + the failure is logged
    so the operator notices."""
    def boom() -> set[str]:
        raise RuntimeError("synthetic provider failure")

    original = _SUPPORTED_DATASETS_PROVIDERS.get("twse")
    try:
        register_supported_datasets("twse", boom)
        with caplog.at_level("ERROR"):
            assert supported_datasets("twse") == set()
        # Log message names the source so an operator can find it.
        assert any(
            "supported_datasets" in r.message and "twse" in r.message
            for r in caplog.records
        )
    finally:
        if original is not None:
            register_supported_datasets("twse", original)


def test_register_supported_datasets_override_works():
    """Mirrors `test_register_connector_overrides_default` for the
    supported-datasets registry — real Phase B modules must be able
    to declare their coverage at import time."""
    original = _SUPPORTED_DATASETS_PROVIDERS.get("tpex")
    try:
        register_supported_datasets(
            "tpex", lambda: {"TaiwanStockConvertibleBondDaily"},
        )
        assert "TaiwanStockConvertibleBondDaily" in supported_datasets("tpex")
        assert covers_dataset(
            "tpex", "TaiwanStockConvertibleBondDaily",
        ) is True
    finally:
        if original is not None:
            register_supported_datasets("tpex", original)
        else:
            _SUPPORTED_DATASETS_PROVIDERS.pop("tpex", None)


@pytest.mark.parametrize("source", ["finmind", "twse", "mops"])
def test_real_sources_return_string_codes_only(source):
    """Defensive: every entry in the set must be a non-empty string
    (catches accidental None / tuple pollution from a future
    refactor)."""
    rows = supported_datasets(source)
    assert all(isinstance(c, str) and c for c in rows)
