"""
APScheduler setup — AsyncIOScheduler runs inside the FastAPI event loop.
Jobs are registered here and started/stopped via the lifespan hook in main.py.
"""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

scheduler = AsyncIOScheduler(timezone="UTC")


def setup_jobs() -> None:
    from tasks.us_market_refresh import (
        refresh_sp500_universe,
        refresh_us_quotes,
        refresh_us_screener,
    )
    from tasks.tw_market_refresh import refresh_tw_quotes, refresh_tw_symbol_map
    from tasks.crypto_market_refresh import refresh_crypto_quotes

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

    # Screener cache warm — every 5 min. Runs the FULL waterfall
    # (Polygon → yfinance → Stooq) including Stooq's slow ~20 s walk
    # over the curated universe so user requests always hit a warm
    # cache instead of timing out at the proxy.
    scheduler.add_job(
        refresh_us_screener,
        trigger=IntervalTrigger(minutes=5),
        id="us_screener_warm",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
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

    # TW ETF yields — recompute trailing-12-month dividend yield for every
    # ETF nightly at 23:30 Asia/Taipei (15:30 UTC). BWIBBU_ALL doesn't
    # cover ETFs, so the screener's yield filter would otherwise reject
    # every ETF (the "高息 ETF" preset returned 0 results without this).
    from tasks.tw_etf_yields_refresh import refresh_tw_etf_yields
    scheduler.add_job(
        refresh_tw_etf_yields,
        trigger=CronTrigger(hour=15, minute=30, timezone="UTC"),
        id="tw_etf_yields",
        replace_existing=True,
        max_instances=1,
    )

    # ── Crypto market quote polling ───────────────────────────────
    # Every 30s; 24/7, no trading-hours skip. Will be replaced by the
    # Kraken WebSocket pump in Week 3 (then this becomes an idle no-op).
    scheduler.add_job(
        refresh_crypto_quotes,
        trigger=IntervalTrigger(seconds=30),
        id="crypto_quotes",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ── Portfolio EOD snapshots ───────────────────────────────────
    # Run at 23:00 UTC daily (after US + TW markets have both closed)
    from tasks.portfolio_snapshot import take_all_snapshots
    scheduler.add_job(
        take_all_snapshots,
        trigger=CronTrigger(hour=23, minute=0, timezone="UTC"),
        id="portfolio_snapshots",
        replace_existing=True,
        max_instances=1,
    )

    # ── GitHub release polling ────────────────────────────────────
    # Pre-warms the Redis cache so GET /api/system/version is fast.
    from config import settings
    from services.version_service import refresh_release_cache
    scheduler.add_job(
        refresh_release_cache,
        trigger=IntervalTrigger(hours=settings.UPDATE_CHECK_INTERVAL_HOURS),
        id="github_release_check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
