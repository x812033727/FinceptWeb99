"""
Dedicated scheduler process — hosts APScheduler + the Kraken WS pump
without serving HTTP.

    python worker.py

Compose runs this as the `scheduler` service while web workers set
SCHEDULER_ENABLED=false, so the ~34 background jobs run exactly once
per deployment instead of once per uvicorn worker. Quote-polling jobs
read the cross-worker subscription registry in Redis (see
api/websocket/manager.py) so this process polls for every web worker's
clients despite holding zero WebSocket connections itself.

Alert delivery: this process publishes to the `user:alerts` channel;
web workers' pubsub listeners do the socket delivery. No listener runs
here — there are no sockets to deliver to.

Shares the exact same image/config as the backend; only the entrypoint
differs.
"""
import asyncio
import logging
import signal

import sqlalchemy

from cache.redis_cache import ping as redis_ping
from db.session import engine
from logging_config import setup_logging

setup_logging()
log = logging.getLogger("worker")


async def main() -> None:
    # ── Fail-fast dependency probes (same contract as main.py) ────
    async with engine.connect() as conn:
        await conn.execute(sqlalchemy.text("SELECT 1"))
    if not await redis_ping():
        raise RuntimeError("Cannot connect to Redis")

    from api.websocket.manager import publish_alert_to_user, publish_update
    from data.crypto.kraken_ws import KrakenTickerPump
    from services.notification_service import register_push_impl
    from tasks.scheduler import scheduler, setup_jobs

    register_push_impl(publish_alert_to_user)
    setup_jobs()
    scheduler.start()

    # Warmups: the scheduled jobs in THIS process iterate the TW symbol
    # map (e.g. ETF yields) and the S&P 500 universe, so warm this
    # process's module caches the same way main.py warms each worker's.
    from services.tw_market_service import (
        load_symbol_map_from_cache,
        load_symbol_map_from_db,
        refresh_symbol_map,
    )
    from tasks.tw_etf_yields_refresh import warmup_tw_etf_yields
    from data.us.sp500_universe import get_sp500_tickers

    async def _tw_warmup() -> None:
        if not await load_symbol_map_from_cache():
            await load_symbol_map_from_db()
        await refresh_symbol_map()
        await warmup_tw_etf_yields()

    asyncio.create_task(_tw_warmup())
    asyncio.create_task(get_sp500_tickers())

    crypto_pump = KrakenTickerPump(publish_update)
    crypto_pump.start()

    log.info("scheduler worker up: %d jobs registered", len(scheduler.get_jobs()))

    # ── Run until SIGTERM/SIGINT, then drain gracefully ───────────
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("scheduler worker shutting down")
    await crypto_pump.stop()
    scheduler.shutdown(wait=False)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
