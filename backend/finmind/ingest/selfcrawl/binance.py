"""Binance self-crawl connector — crypto OHLCV for the FinMind clone
ingest pipeline.

Crypto datasets are "born self-sourced": their
`dataset_sources.active_source` starts at 'binance' (FinMind has no
crypto data), so unlike the TW Phase-B connectors there's no 'finmind'
fallback to diff against — the runner routes straight here.

Wraps `data.crypto.binance_connector` into the `SourceClient` shape.
`symbol` is the Binance pair (e.g. BTCUSDT), supplied per-chunk by the
crypto universe fan-out in the scheduler. Handlers return rows in the
raw shape the `crypto_ohlcv` DatasetMapping's column_map + row_transform
expect (the connector already emits FinMind-clone-friendly keys, so the
wrapper is a thin interval selector).

Datasets handled:
  - CryptoPrice         daily klines  (interval '1d')
  - CryptoPriceHourly   hourly klines (interval '1h')
  - CryptoFundingRate   USDT-perp funding rate (8h cadence)
  - CryptoOpenInterest  USDT-perp open interest (~30d history only)
"""
from __future__ import annotations

from datetime import date
from typing import Any

from data.crypto import binance_connector as _binance


async def _fetch_crypto_price_daily(
    symbol: str | None, start_date: date, end_date: date,
) -> list[dict[str, Any]]:
    if not symbol:
        return []
    return await _binance.fetch_ohlcv(symbol, "1d", start_date, end_date)


async def _fetch_crypto_price_hourly(
    symbol: str | None, start_date: date, end_date: date,
) -> list[dict[str, Any]]:
    if not symbol:
        return []
    return await _binance.fetch_ohlcv(symbol, "1h", start_date, end_date)


async def _fetch_crypto_funding_rate(
    symbol: str | None, start_date: date, end_date: date,
) -> list[dict[str, Any]]:
    # Uses the USDT-perp of the same symbol string (Binance perps reuse
    # the spot pair name, e.g. BTCUSDT). Coins without a perp return []
    # (recorded as done/0 rows), which is the correct no-op.
    if not symbol:
        return []
    return await _binance.fetch_funding_rate(symbol, start_date, end_date)


async def _fetch_crypto_open_interest(
    symbol: str | None, start_date: date, end_date: date,
) -> list[dict[str, Any]]:
    if not symbol:
        return []
    return await _binance.fetch_open_interest(symbol, start_date, end_date)


# ── Dispatch table ──────────────────────────────────────────────

_DISPATCH = {
    "CryptoPrice": _fetch_crypto_price_daily,
    "CryptoPriceHourly": _fetch_crypto_price_hourly,
    "CryptoFundingRate": _fetch_crypto_funding_rate,
    "CryptoOpenInterest": _fetch_crypto_open_interest,
}


def supported_datasets() -> list[str]:
    """Crypto datasets the Binance wrapper handles."""
    return sorted(_DISPATCH.keys())


class BinanceClient:
    """Implements `SourceClient` via `data.crypto.binance_connector`."""

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
                f"BinanceClient has no handler for {dataset_code}. "
                f"Add a `_fetch_*` and register in "
                f"finmind/ingest/selfcrawl/binance.py:_DISPATCH."
            )
        return await handler(symbol, start_date, end_date)
