"""Refresh the crypto universe + market-cap snapshot.

Weekly maintenance job (outside the per-dataset ingest framework
because it does a cross-source CoinGecko→Binance join). Steps:

  1. CoinGecko top-N by market cap  → the coins we track.
  2. Binance spot exchangeInfo      → which of those have a USDT pair.
  3. Upsert `crypto_universe`: each coin gets `binance_symbol`
     (`<SYMBOL>USDT` when Binance lists it, else NULL) and a `status`
     — 'active' (tradable pair), 'unmapped' (no Binance pair, or a
     stablecoin we skip), 'delisted' (fell out of top-N since last run).

The scheduler's `get_crypto_universe` reads active binance_symbols from
this table to fan out the per-symbol crypto datasets.

The market-cap / supply *history* (crypto_asset_info) is owned by the
`CryptoInfo` catalog dataset (a per-run CoinGecko snapshot through the
normal ingest path) — not this script — so there's a single writer per
table.

Usage (from `backend/`):
    python -m finmind.scripts.crypto_universe_refresh            # top 200
    python -m finmind.scripts.crypto_universe_refresh --top 50
    python -m finmind.scripts.crypto_universe_refresh --dry-run  # print, no write
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from sqlalchemy import bindparam, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

# Stablecoins / wrapped-BTC that technically have a Binance USDT pair but
# whose "price history" is a flat line or a proxy — mark 'unmapped'
# (info-only) rather than fetching pointless klines for them.
_SKIP_SYMBOLS: frozenset[str] = frozenset({
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDD", "PYUSD",
    "WBTC", "WETH", "WBETH", "STETH", "WEETH", "BSC-USD",
})


def build_rows(
    markets: list[dict[str, Any]],
    spot_symbols: set[str],
    today: date,
    *,
    skip_symbols: frozenset[str] = _SKIP_SYMBOLS,
) -> list[dict[str, Any]]:
    """Pure core: turn a CoinGecko markets list + Binance spot symbol set
    into crypto_universe rows. No I/O — unit-testable.

    A coin maps to Binance iff `<SYMBOL>USDT` is a live spot pair AND the
    symbol isn't in `skip_symbols`. Mapped → status 'active'; otherwise
    'unmapped' with binance_symbol=None."""
    universe_rows: list[dict[str, Any]] = []
    for m in markets:
        cid = m.get("coingecko_id")
        if not cid:
            continue
        sym = (m.get("symbol") or "").upper()
        candidate = f"{sym}USDT"
        mapped = sym not in skip_symbols and candidate in spot_symbols
        universe_rows.append({
            "coingecko_id": cid,
            "symbol": sym,
            "name": m.get("name"),
            "binance_symbol": candidate if mapped else None,
            "exchange": "binance",
            "status": "active" if mapped else "unmapped",
            "market_cap_rank": m.get("market_cap_rank"),
            # Pass a date object, not an ISO string: Postgres binds a
            # DATE column from a python date, but rejects a str via raw
            # text() (SQLite is lenient, which is why unit tests missed
            # this). Same reason `today` below stays a date.
            "added_at": today,
            "source": "coingecko",
        })
    return universe_rows


# ON CONFLICT preserves added_at (first-seen) and clears removed_at when a
# coin re-enters the tracked set; only the mutable descriptors refresh.
_UNIVERSE_UPSERT = text(
    "INSERT INTO crypto_universe "
    "(coingecko_id, symbol, name, binance_symbol, exchange, status, "
    " market_cap_rank, added_at, removed_at, source, ingested_at) "
    "VALUES (:coingecko_id, :symbol, :name, :binance_symbol, :exchange, "
    " :status, :market_cap_rank, :added_at, NULL, :source, CURRENT_TIMESTAMP) "
    "ON CONFLICT (coingecko_id) DO UPDATE SET "
    " symbol=EXCLUDED.symbol, name=EXCLUDED.name, "
    " binance_symbol=EXCLUDED.binance_symbol, exchange=EXCLUDED.exchange, "
    " status=EXCLUDED.status, market_cap_rank=EXCLUDED.market_cap_rank, "
    " removed_at=NULL, source=EXCLUDED.source, ingested_at=CURRENT_TIMESTAMP"
)

async def apply_refresh(
    session: AsyncSession,
    universe_rows: list[dict[str, Any]],
    today: date,
) -> dict[str, int]:
    """Upsert the universe rows and mark coins that fell out of the
    tracked set as delisted. Returns a small counts summary."""
    if universe_rows:
        await session.execute(_UNIVERSE_UPSERT, universe_rows)

    current_ids = [r["coingecko_id"] for r in universe_rows]
    delisted = 0
    if current_ids:
        result = await session.execute(
            text(
                "UPDATE crypto_universe SET status='delisted', "
                "removed_at=:today "
                "WHERE status IN ('active','unmapped') "
                "AND coingecko_id NOT IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"today": today, "ids": current_ids},
        )
        delisted = result.rowcount or 0
    await session.commit()
    return {
        "universe": len(universe_rows),
        "active": sum(1 for r in universe_rows if r["status"] == "active"),
        "delisted": delisted,
    }


async def amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--top", type=int, default=200,
                        help="Number of top-market-cap coins to track (default 200).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + print the plan without writing to the DB.")
    args = parser.parse_args()

    from data.crypto import binance_connector as binance
    from data.crypto import coingecko_connector as coingecko

    markets = await coingecko.get_markets(args.top)
    if not markets:
        print("crypto_universe_refresh: CoinGecko returned no markets — "
              "skipping (rate-limited or down).", file=sys.stderr)
        return 1
    spot = await binance.get_spot_usdt_symbols()
    today = datetime.now(tz=timezone.utc).date()
    universe_rows = build_rows(markets, spot, today)
    active = sum(1 for r in universe_rows if r["status"] == "active")

    if args.dry_run:
        print(f"# crypto_universe_refresh dry-run — {len(universe_rows)} coins, "
              f"{active} mapped to Binance")
        for r in universe_rows[:20]:
            print(f"  {r['market_cap_rank']:>4} {r['symbol']:<8} "
                  f"{r['status']:<9} {r['binance_symbol'] or '-'}")
        if len(universe_rows) > 20:
            print(f"  … +{len(universe_rows) - 20} more")
        return 0

    from finmind.db.session import FinmindAsyncSessionLocal
    async with FinmindAsyncSessionLocal() as session:
        summary = await apply_refresh(session, universe_rows, today)
    print(f"crypto_universe_refresh: {summary}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
