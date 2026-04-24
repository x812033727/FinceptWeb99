"""
APScheduler setup — AsyncIOScheduler runs inside the FastAPI event loop.
Jobs are registered here and started/stopped via the lifespan hook in main.py.
"""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

scheduler = AsyncIOScheduler(timezone="UTC")


def setup_jobs() -> None:
    from tasks.us_market_refresh import refresh_us_quotes, refresh_sp500_universe
    from tasks.tw_market_refresh import refresh_tw_quotes, refresh_tw_symbol_map

    # ── US market quote polling ───────────────────────────────────
    # Every 10s during market hours; job itself checks and skips outside hours
    scheduler.add_job(
        refresh_us_quotes,
        trigger=IntervalTrigger(seconds=10),
        id="us_quotes",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # S&P 500 universe refresh — once daily at 06:00 UTC (pre-market)
    scheduler.add_job(
        refresh_sp500_universe,
        trigger=IntervalTrigger(hours=24),
        id="sp500_universe",
        replace_existing=True,
        max_instances=1,
    )

    # ── TW market quote polling ───────────────────────────────────
    # Every 60s; job skips outside 09:00-13:30 CST
    scheduler.add_job(
        refresh_tw_quotes,
        trigger=IntervalTrigger(seconds=60),
        id="tw_quotes",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # TW symbol→exchange map refresh — once daily at 07:00 UTC
    scheduler.add_job(
        refresh_tw_symbol_map,
        trigger=IntervalTrigger(hours=24),
        id="tw_symbol_map",
        replace_existing=True,
        max_instances=1,
    )
