"""CoinGecko self-crawl connector — crypto market-cap info for the
FinMind clone ingest pipeline.

Handles the market-wide `CryptoInfo` dataset (per_symbol=False): one
CoinGecko /coins/markets call per run returns the top-N coins, and the
handler stamps the chunk's date as `snapshot_date` so each run appends a
dated snapshot to `crypto_asset_info` (PK snapshot_date, coingecko_id).

Wraps `data.crypto.coingecko_connector`. Distinct from the
`crypto_universe_refresh` maintenance script, which owns the
`crypto_universe` routing table (CoinGecko→Binance symbol mapping);
this connector only feeds the append-only info snapshot that the public
catalog serves.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from data.crypto import coingecko_connector as _coingecko

# How many top-market-cap coins the CryptoInfo snapshot covers. Matches
# the tracked universe size so the info table and the OHLCV universe stay
# aligned.
CRYPTO_INFO_TOP_N = 200


async def _fetch_crypto_info(
    symbol: str | None, start_date: date, end_date: date,
) -> list[dict[str, Any]]:
    """Market-wide — `symbol` is ignored. Stamps `snapshot_date` =
    `end_date` (the chunk's as-of date) onto every coin row so the
    mapping's PK (snapshot_date, coingecko_id) lands one dated snapshot
    per run."""
    markets = await _coingecko.get_markets(CRYPTO_INFO_TOP_N)
    snapshot = end_date.isoformat()
    for row in markets:
        row["snapshot_date"] = snapshot
    return markets


_DISPATCH = {
    "CryptoInfo": _fetch_crypto_info,
}


def supported_datasets() -> list[str]:
    return sorted(_DISPATCH.keys())


class CoingeckoClient:
    """Implements `SourceClient` via `data.crypto.coingecko_connector`."""

    async def fetch(
        self,
        dataset_code: str,
        symbol: str | None,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        handler = _DISPATCH.get(dataset_code)
        if handler is None:
            raise NotImplementedError(
                f"CoingeckoClient has no handler for {dataset_code}. "
                f"Add a `_fetch_*` and register in "
                f"finmind/ingest/selfcrawl/coingecko.py:_DISPATCH."
            )
        return await handler(symbol, start_date, end_date)
