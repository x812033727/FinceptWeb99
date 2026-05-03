"""
APScheduler setup — AsyncIOScheduler runs inside the FastAPI event loop.
Jobs are registered here and started/stopped via the lifespan hook in main.py.
"""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

scheduler = AsyncIOScheduler(timezone="UTC")


def setup_jobs() -> None:
    from tasks.crypto_market_refresh import refresh_crypto_quotes
    from tasks.tw_market_refresh import refresh_tw_quotes, refresh_tw_symbol_map
    from tasks.us_market_refresh import (
        refresh_sp500_universe,
        refresh_us_quotes,
        refresh_us_screener,
    )

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

    # ── TW EOD quote refresh ─────────────────────────────────────
    # 08:30 UTC = 16:30 Asia/Taipei (3 h after TW close at 13:30
    # Taipei). Forces one final pass over WS-subscribed symbols so
    # the Redis cache holds the post-close settled prices — without
    # this, watchlist views opened in the evening serve the last
    # tick from 13:30 until next morning's 09:00 polling resumes.
    from tasks.tw_market_refresh import refresh_tw_quotes_eod
    scheduler.add_job(
        refresh_tw_quotes_eod,
        trigger=CronTrigger(hour=8, minute=30, timezone="UTC"),
        id="tw_quotes_eod",
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

    # ── TW OHLCV → Postgres archive ───────────────────────────────
    # Daily after TWSE close (14:30 Asia/Taipei = 06:30 UTC). Populates
    # the ohlcv_daily table so the read path can serve K-lines from DB
    # when both TWSE and FinMind are unavailable simultaneously.
    from tasks.ingest_ohlcv_tw import run as run_ingest_ohlcv_tw
    scheduler.add_job(
        run_ingest_ohlcv_tw,
        trigger=CronTrigger(hour=6, minute=30, timezone="UTC"),
        id="ingest_ohlcv_tw",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ── TW fundamentals → Postgres archive ────────────────────────
    # Daily 06:45 UTC (right after the OHLCV ingest at 06:30). Single
    # TWSE BWIBBU_ALL call returns the full day's PE / PB / dividend
    # yield cross-section, so this is cheap — no per-symbol fan-out and
    # no quota pressure.
    from tasks.ingest_fundamentals_tw import run as run_ingest_fundamentals_tw
    scheduler.add_job(
        run_ingest_fundamentals_tw,
        trigger=CronTrigger(hour=6, minute=45, timezone="UTC"),
        id="ingest_fundamentals_tw",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ── TW news → Postgres archive ────────────────────────────────
    # Hourly. One FinMind TaiwanStockNews call per hour returns the
    # market-wide cross-section in a single request, so this stays
    # well under FinMind's 600/day quota (~24 calls/day).
    from tasks.ingest_news_tw import run as run_ingest_news_tw
    scheduler.add_job(
        run_ingest_news_tw,
        trigger=IntervalTrigger(hours=1),
        id="ingest_news_tw",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Same Google News RSS pipeline, zh-TW edition, but the query
    # targets Fed / FOMC / 美股 / 國際財經 and rows are stored under
    # market='GLOBAL'. Lets discussion personas reason about Fed
    # policy / global macro alongside TW market context. Hourly to
    # match the TW cadence.
    from tasks.ingest_news_international import run as run_ingest_news_intl
    scheduler.add_job(
        run_ingest_news_intl,
        trigger=IntervalTrigger(hours=1),
        id="ingest_news_international",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ── TW chip metrics ───────────────────────────────────────────
    # Both fire post-close (TWSE close 13:30 Taipei = 05:30 UTC).
    # Slightly staggered so a slow-but-not-yet-stuck TWSE doesn't
    # have two parallel queries hammering it. One TWSE call covers
    # the whole market in each task — no per-symbol fan-out.
    from tasks.ingest_institutional_tw import run as run_ingest_institutional_tw
    scheduler.add_job(
        run_ingest_institutional_tw,
        trigger=CronTrigger(hour=6, minute=50, timezone="UTC"),
        id="ingest_institutional_tw",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    from tasks.ingest_margin_tw import run as run_ingest_margin_tw
    scheduler.add_job(
        run_ingest_margin_tw,
        trigger=CronTrigger(hour=7, minute=0, timezone="UTC"),
        id="ingest_margin_tw",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # TAIEX 大盤加權指數 daily history. One FMTQIK call per day
    # returns the full month's index OHLC; idempotent UPSERT into
    # ohlcv_daily under symbol='_TAIEX' so the existing read tier
    # serves it. Stays after the chip-metric tasks so all post-close
    # writes happen in one ~20-min window.
    from tasks.ingest_taiex_history import run as run_ingest_taiex_history
    scheduler.add_job(
        run_ingest_taiex_history,
        trigger=CronTrigger(hour=7, minute=10, timezone="UTC"),
        id="ingest_taiex_history",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # 月營收 (monthly revenue). TW securities law gives companies
    # until the 10th of the following month to publish; running daily
    # picks up late filers + corrections without needing a precise
    # "everyone's filed" tick.
    #
    # FinMind market-wide one-call query is the primary path (1 call
    # per day returns every listed company's last 90 days). Worked
    # free until 2026-04 when FinMind moved the dataset to paid-
    # sponsor-only; PR #183 added a paywall fail-soft so the cron
    # marks itself `skipped:` rather than alarming. As of $sponsor-
    # paid the dataset is back open — no code change needed; the
    # detector only triggers on the paywall response body.
    from tasks.ingest_revenue_tw import run as run_ingest_revenue_tw
    scheduler.add_job(
        run_ingest_revenue_tw,
        trigger=CronTrigger(hour=9, minute=0, timezone="UTC"),
        id="ingest_revenue_tw",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # `tasks.ingest_revenue_tw_slow` (the MOPS per-symbol fan-out
    # built in PR #184 as a paywall fallback) is intentionally NOT
    # scheduled here. The task file + admin retry button are still
    # wired so the cron can be revived by hand if FinMind has a
    # multi-day outage / quota issue. Re-add this block to put it
    # back on a schedule:
    #
    #   from tasks.ingest_revenue_tw_slow import run as run_slow
    #   scheduler.add_job(run_slow, trigger=IntervalTrigger(hours=1),
    #                     id="ingest_revenue_tw_slow",
    #                     replace_existing=True, max_instances=1,
    #                     coalesce=True)

    # 庫藏股 (TW listed-company buyback announcements). The cron is
    # intentionally NOT scheduled — FinMind v4 doesn't expose a
    # `TaiwanStockBuyBack` dataset (verified via direct curl + the
    # tutor docs), every call returns HTTP 422 and arms backoff. The
    # task file + connector method are kept so the cron can be revived
    # by hand if FinMind ever adds the dataset. Re-add this block to
    # put it back on a schedule:
    #
    #   from tasks.ingest_buyback_tw import run as run_ingest_buyback_tw
    #   scheduler.add_job(run_ingest_buyback_tw,
    #                     trigger=CronTrigger(hour=10, minute=0, timezone="UTC"),
    #                     id="ingest_buyback_tw",
    #                     replace_existing=True, max_instances=1, coalesce=True)

    # 八大行庫 daily flow. Sponsor-tier FinMind dataset; one
    # market-wide call/day. 18:30 Taipei (10:30 UTC), 30 min after
    # buyback so the sponsor-tier cluster spreads predictably.
    from tasks.ingest_govt_bank_flow_tw import (
        run as run_ingest_govt_bank_flow_tw,
    )
    scheduler.add_job(
        run_ingest_govt_bank_flow_tw,
        trigger=CronTrigger(hour=10, minute=30, timezone="UTC"),
        id="ingest_govt_bank_flow_tw",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # 個股期貨 三大法人未平倉 (PR #282). FinMind sponsor-tier
    # `TaiwanFuturesInstitutionalInvestors` market-wide call.
    # 18:45 Taipei (10:45 UTC), 15 min after govt_bank_flow to
    # avoid stacking sponsor-tier requests on a single tick.
    from tasks.ingest_stock_futures_oi_tw import (
        run as run_ingest_stock_futures_oi_tw,
    )
    scheduler.add_job(
        run_ingest_stock_futures_oi_tw,
        trigger=CronTrigger(hour=10, minute=45, timezone="UTC"),
        id="ingest_stock_futures_oi_tw",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # TAIEX TR (含息報酬指數). FinMind sponsor-tier; pulls trailing
    # 400 days every tick (cheap, idempotent UPSERT). 15:30 Taipei
    # (07:30 UTC), 20 min after `ingest_taiex_history` so neither
    # shares the same TWSE quiet window. Archive lands under the
    # synthetic symbol `_TAIEX_TR` in `ohlcv_daily` — same layout
    # as `_TAIEX` from PR #132.
    from tasks.ingest_taiex_tr_history import (
        run as run_ingest_taiex_tr_history,
    )
    scheduler.add_job(
        run_ingest_taiex_tr_history,
        trigger=CronTrigger(hour=7, minute=30, timezone="UTC"),
        id="ingest_taiex_tr_history",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Risk-signals 三件套 (處置 / 暫停 / 當沖). FinMind sponsor-tier,
    # 3 datasets per tick. 19:00 Taipei (11:00 UTC), after the
    # buyback / govt-bank cluster.
    from tasks.ingest_risk_signals_tw import (
        run as run_ingest_risk_signals_tw,
    )
    scheduler.add_job(
        run_ingest_risk_signals_tw,
        trigger=CronTrigger(hour=11, minute=0, timezone="UTC"),
        id="ingest_risk_signals_tw",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # 股權分散 + 全市場三大法人. FinMind sponsor-tier, 2 datasets
    # per tick. 19:30 Taipei (11:30 UTC), after risk-signals.
    from tasks.ingest_holdings_aggregates_tw import (
        run as run_ingest_holdings_aggregates_tw,
    )
    scheduler.add_job(
        run_ingest_holdings_aggregates_tw,
        trigger=CronTrigger(hour=11, minute=30, timezone="UTC"),
        id="ingest_holdings_aggregates_tw",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ── News sentiment scoring ────────────────────────────────────
    # Hourly: picks up news rows with NULL sentiment_score and runs them
    # through Claude Haiku in batches. Cheap (one prompt scores 20
    # headlines) and gives the discussion orchestrator pre-computed
    # sentiment context. Job has its own Redis lock so two pods can't
    # race the same rows.
    from tasks.score_news_sentiment import run as run_score_news_sentiment
    scheduler.add_job(
        run_score_news_sentiment,
        trigger=IntervalTrigger(minutes=30),
        id="score_news_sentiment",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ── Signal-audit daily snapshot (PR #263) ─────────────────────
    # Once per UTC day at 02:30 — runs after the daily auto-run
    # discussions (00:00) + the news sentiment scorer's first cycle
    # so the snapshot reflects fully-scored data. Persists per-signal
    # citation / value-citation / hallucination stats into
    # `signal_audit_history` for the AdminPage sparkline column.
    from tasks.snapshot_signal_audit import run as run_snapshot_signal_audit
    scheduler.add_job(
        run_snapshot_signal_audit,
        trigger=CronTrigger(hour=2, minute=30, timezone="UTC"),
        id="snapshot_signal_audit",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ── TW quote_snapshots retention prune ────────────────────────
    # Daily at 03:00 UTC; deletes rows older than 30 days so the table
    # doesn't grow unbounded under busy WS subscriptions.
    from tasks.ingest_quotes_retention_tw import run as run_quotes_retention_tw
    scheduler.add_job(
        run_quotes_retention_tw,
        trigger=CronTrigger(hour=3, minute=0, timezone="UTC"),
        id="ingest_quotes_retention_tw",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ── discussion_round_contexts retention prune ─────────────────
    # Daily at 04:00 UTC (offset by 1h from quote retention to avoid
    # a synchronised DELETE peak). Deletes round-context snapshots
    # older than 90 days — same window as `_PRIOR_DISCUSSIONS_LOOKBACK
    # _DAYS` so the replay archive ages out together with the cross-
    # session memory window. Discussions + turns rows are kept forever.
    from tasks.prune_discussion_contexts import run as run_prune_discussion_contexts
    scheduler.add_job(
        run_prune_discussion_contexts,
        trigger=CronTrigger(hour=4, minute=0, timezone="UTC"),
        id="prune_discussion_contexts",
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

    # ── Auto-run discussion ───────────────────────────────────────
    # Daily at 00:00 UTC = 08:00 Asia/Taipei (1h before TW market opens
    # at 09:00 Taipei). Job creates a 5-round expert discussion under
    # the admin user, runs it to completion, captures the synthesizer
    # picks, and seeds verify_after_date so the verifier task can grade
    # it 5 trading days later. Internal trading-day gate skips weekends.
    from tasks.auto_run_discussion import run as run_auto_run_discussion
    scheduler.add_job(
        run_auto_run_discussion,
        trigger=CronTrigger(hour=0, minute=0, timezone="UTC"),
        id="auto_run_discussion",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ── Verify discussion outcome ─────────────────────────────────
    # Daily at 08:30 UTC = 16:30 Asia/Taipei (3h after TW market closes
    # at 13:30 Taipei). Picks up auto-run rows whose 5-day window has
    # elapsed and stamps `verdict` (win/loss/unverifiable) based on
    # whether any recommended symbol's max(high) exceeded day1_open ×
    # 1.03 inside the window.
    from tasks.verify_discussion_outcome import run as run_verify_discussion
    scheduler.add_job(
        run_verify_discussion,
        trigger=CronTrigger(hour=8, minute=30, timezone="UTC"),
        id="verify_discussion_outcome",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ── Score discussion outcomes (D1-D5 closes) ──────────────────
    # Daily at 09:30 UTC. Sibling of verify_discussion_outcome but
    # populates the per-day close array (`daily_close_prices`) that
    # backs the "對答案" UI on the discussion detail page. Runs an
    # hour after verify so any day1_open captured by the verifier is
    # already in place before we read it. Covers BOTH manual and
    # auto-run discussions (vs verifier which only touches auto_run=
    # True rows).
    from tasks.score_discussion_outcomes import run as run_score_discussion_outcomes
    scheduler.add_job(
        run_score_discussion_outcomes,
        trigger=CronTrigger(hour=9, minute=30, timezone="UTC"),
        id="score_discussion_outcomes",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
