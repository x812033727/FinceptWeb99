"""Top 20 crypto symbols supported on launch.

Hand-curated to match Kraken's spot pairs against USD. Operators can
extend later by adding to TOP20 + the corresponding entry in
KRAKEN_PAIR_MAP.

Naming: we use canonical ticker (BTC, ETH, ...) as the *symbol* the rest
of the system sees. Kraken historically uses XBT instead of BTC and
prefixes some assets with X / Z (legacy convention from when they
matched ISO 4217 currency codes). KRAKEN_PAIR_MAP translates between
the two so the rest of the code never has to know about XBT.
"""
from __future__ import annotations

# Canonical symbols the rest of the app sees (uppercase, no exchange suffix).
TOP20: tuple[str, ...] = (
    "BTC", "ETH", "SOL", "XRP", "BNB",
    "DOGE", "ADA", "AVAX", "TRX", "LINK",
    "DOT", "MATIC", "LTC", "BCH", "NEAR",
    "UNI", "ATOM", "XLM", "FIL", "OP",
)

# Display names — used for news search queries and the StockDetailPage
# header so we can render `Bitcoin` not `BTC` next to the price.
NAMES: dict[str, str] = {
    "BTC":   "Bitcoin",
    "ETH":   "Ethereum",
    "SOL":   "Solana",
    "XRP":   "XRP",
    "BNB":   "BNB",
    "DOGE":  "Dogecoin",
    "ADA":   "Cardano",
    "AVAX":  "Avalanche",
    "TRX":   "TRON",
    "LINK":  "Chainlink",
    "DOT":   "Polkadot",
    "MATIC": "Polygon",
    "LTC":   "Litecoin",
    "BCH":   "Bitcoin Cash",
    "NEAR":  "NEAR Protocol",
    "UNI":   "Uniswap",
    "ATOM":  "Cosmos",
    "XLM":   "Stellar",
    "FIL":   "Filecoin",
    "OP":    "Optimism",
}

# Kraken's REST/WS pair format. We always quote against USD.
# The REST API accepts these on /0/public/Ticker?pair=... and returns the
# canonical key in the response, which may or may not match the request
# (Kraken normalizes some pairs e.g. BTCUSD → XXBTZUSD on response).
_KRAKEN_PAIR: dict[str, str] = {
    "BTC":   "XBTUSD",
    "ETH":   "ETHUSD",
    "SOL":   "SOLUSD",
    "XRP":   "XRPUSD",
    "BNB":   "BNBUSD",     # Kraken actually doesn't list BNB; tracked as gap
    "DOGE":  "XDGUSD",     # Kraken's legacy XDG ticker
    "ADA":   "ADAUSD",
    "AVAX":  "AVAXUSD",
    "TRX":   "TRXUSD",
    "LINK":  "LINKUSD",
    "DOT":   "DOTUSD",
    "MATIC": "MATICUSD",
    "LTC":   "LTCUSD",
    "BCH":   "BCHUSD",
    "NEAR":  "NEARUSD",
    "UNI":   "UNIUSD",
    "ATOM":  "ATOMUSD",
    "XLM":   "XLMUSD",
    "FIL":   "FILUSD",
    "OP":    "OPUSD",
}

# Reverse: response keys Kraken normalizes to → our canonical symbol.
# Built lazily because we have to handle Kraken's "X" / "Z" prefix variants
# that show up in response payloads even when the request used the short form.
_KRAKEN_RESPONSE_KEYS: dict[str, str] = {
    "XXBTZUSD": "BTC", "XBTUSD": "BTC",
    "XETHZUSD": "ETH", "ETHUSD": "ETH",
    "SOLUSD": "SOL",
    "XXRPZUSD": "XRP", "XRPUSD": "XRP",
    "BNBUSD": "BNB",
    "XDGUSD": "DOGE", "XXDGZUSD": "DOGE",
    "ADAUSD": "ADA",
    "AVAXUSD": "AVAX",
    "TRXUSD": "TRX",
    "LINKUSD": "LINK",
    "DOTUSD": "DOT",
    "MATICUSD": "MATIC",
    "XLTCZUSD": "LTC", "LTCUSD": "LTC",
    "BCHUSD": "BCH",
    "NEARUSD": "NEAR",
    "UNIUSD": "UNI",
    "ATOMUSD": "ATOM",
    "XXLMZUSD": "XLM", "XLMUSD": "XLM",
    "FILUSD": "FIL",
    "OPUSD": "OP",
}


def to_kraken_pair(symbol: str) -> str | None:
    """Translate canonical symbol → Kraken request pair string."""
    return _KRAKEN_PAIR.get(symbol.upper())


def from_kraken_response_key(key: str) -> str | None:
    """Translate Kraken response key → canonical symbol."""
    return _KRAKEN_RESPONSE_KEYS.get(key)
