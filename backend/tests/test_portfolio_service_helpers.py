"""
Direct unit tests for the pure helpers in portfolio_service.

The FX-conversion paths (`_to_portfolio_currency`, `get_default_fx_rate`)
are already covered by test_portfolio_service_fx.py and test_portfolio_
historical_fx.py. This file focuses on the two pure helpers underneath
that logic — `_normalize_currency` (stablecoin → USD peg) and
`_market_currency` (market → native trading currency). A regression in
either silently rewrites P&L for cross-currency portfolios.
"""
import pytest

import services.portfolio_service as svc


# ── _normalize_currency ──────────────────────────────────────────

@pytest.mark.parametrize("raw", ["USDT", "USDC", "DAI", "BUSD", "TUSD"])
def test_normalize_currency_maps_known_stablecoins_to_usd(raw):
    """All five tracked USD-pegged stablecoins collapse to "USD" so
    a USDT-bought ETH gets compared against a USD-denominated portfolio
    without an FX round-trip."""
    assert svc._normalize_currency(raw) == "USD"


def test_normalize_currency_passes_usd_through_unchanged():
    assert svc._normalize_currency("USD") == "USD"


def test_normalize_currency_passes_non_stablecoin_currencies_through():
    """Real fiat currencies and unsupported stablecoins must NOT be
    silently rewritten — only the explicit USD-peg list collapses."""
    assert svc._normalize_currency("TWD") == "TWD"
    assert svc._normalize_currency("JPY") == "JPY"
    assert svc._normalize_currency("EUR") == "EUR"
    # An emerging stablecoin we haven't whitelisted yet stays as-is so
    # the FX layer's "unsupported pair → return amount unchanged" path
    # gets a chance to flag it via logging at higher layers.
    assert svc._normalize_currency("FRAX") == "FRAX"


@pytest.mark.parametrize("input,expected", [
    ("usdt", "USD"),
    ("usdc", "USD"),
    ("Dai",  "USD"),
    ("usd",  "USD"),
    ("twd",  "TWD"),
])
def test_normalize_currency_is_case_insensitive(input, expected):
    """Frontend / DB sources have inconsistent casing on currency
    fields (yfinance gives `USD`, some user input goes through as
    `usd`). The helper upper-cases first."""
    assert svc._normalize_currency(input) == expected


# ── _market_currency ─────────────────────────────────────────────

@pytest.mark.parametrize("market,expected", [
    ("US", "USD"),
    ("us", "USD"),
    ("CRYPTO", "USD"),  # crypto is quoted in USD
    ("crypto", "USD"),
    ("TW", "TWD"),
    ("tw", "TWD"),
])
def test_market_currency_maps_market_to_native_quote_currency(market, expected):
    assert svc._market_currency(market) == expected


def test_market_currency_defaults_unknown_markets_to_usd():
    """The only TWD market is TW; everything else defaults to USD.
    This is intentional defensive behaviour — a future "EU" market
    still gets a sensible default while flagging via review that the
    helper needs explicit handling."""
    assert svc._market_currency("EU") == "USD"
    assert svc._market_currency("") == "USD"
    assert svc._market_currency("garbage") == "USD"
