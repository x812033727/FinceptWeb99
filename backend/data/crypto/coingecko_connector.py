"""CoinGecko connector — top-N-by-market-cap coin list + market-cap /
supply info. Drives the crypto universe (which coins we track) and the
weekly crypto_asset_info snapshot.

Free public API, ~30 calls/min — we make one call/week for the top-200
list (per_page=250 covers it), so the limit is a non-issue. Same
fail-soft contract as the Binance connector: warn + return [] on any
error, never raise.

Endpoint:
  GET api.coingecko.com/api/v3/coins/markets
      ?vs_currency=usd&order=market_cap_desc&per_page=250&page=1
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.coingecko.com/api/v3"
_HTTP_TIMEOUT = 20.0


async def _get(path: str, params: dict[str, Any] | None = None) -> Any | None:
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
            r = await c.get(_BASE + path, params=params)
        if r.status_code != 200:
            logger.warning(
                "coingecko.http_error",
                extra={"path": path, "status": r.status_code},
            )
            return None
        return r.json()
    except Exception as exc:
        logger.warning(
            "coingecko.request_failed",
            extra={"path": path, "error": str(exc)},
        )
        return None


async def get_markets(top_n: int = 200) -> list[dict[str, Any]]:
    """Top `top_n` coins by market cap, newest snapshot. Each row →
    `{coingecko_id, symbol (UPPER), name, market_cap_rank, market_cap,
    circulating_supply, total_supply, ath}`. `symbol` is upper-cased so
    it lines up with Binance base assets (BTC, ETH, …). Returns [] on
    failure — callers treat that as "skip this refresh cycle"."""
    per_page = min(max(top_n, 1), 250)
    body = await _get(
        "/coins/markets",
        {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": 1,
            "sparkline": "false",
        },
    )
    if not isinstance(body, list):
        return []
    out: list[dict[str, Any]] = []
    for row in body[:top_n]:
        cid = row.get("id")
        if not cid:
            continue
        out.append({
            "coingecko_id": cid,
            "symbol": (row.get("symbol") or "").upper(),
            "name": row.get("name"),
            "market_cap_rank": row.get("market_cap_rank"),
            "market_cap": row.get("market_cap"),
            "circulating_supply": row.get("circulating_supply"),
            "total_supply": row.get("total_supply"),
            "ath": row.get("ath"),
        })
    return out
