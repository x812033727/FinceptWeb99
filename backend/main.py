import asyncio
import ipaddress
import logging
from contextlib import asynccontextmanager

import sqlalchemy
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from _version import __version__
from api.admin.router import router as admin_router
from api.ai_agents.router import router as ai_router
from api.ai_agents.stock_report import router as stock_report_router
from api.alerts.router import router as alerts_router
from api.analytics.router import router as analytics_router
from api.auth.router import router as auth_router
from api.crypto_market.router import router as crypto_router
from api.discussion.router import router as discussion_router
from api.global_market.router import router as global_router
from api.announcements.router import router as announcements_router
from api.notifications.router import router as notifications_router
from api.portfolio.router import router as portfolio_router
from api.system.router import router as system_router
from api.tw_market.router import router as tw_router
from api.us_market.router import router as us_router
from api.watchlist.router import router as watchlist_router
from api.websocket.router import router as ws_router
# FinMind clone subsystem — separate sub-app under `backend/finmind/`,
# isolated DB. Late-imported here only to mount the router; the
# subsystem doesn't run any startup side-effects in the main process
# beyond engine initialization (lazy connect, no DB roundtrip at
# import time).
from finmind.api.router import router as finmind_router
from cache.redis_cache import ping as redis_ping
from config import settings
from db.seed import seed_admin
from db.session import AsyncSessionLocal, engine
from limiter import limiter
from logging_config import setup_logging
from middleware.etag import ETagMiddleware
from middleware.metrics import PrometheusMiddleware, metrics_endpoint

setup_logging()
log = logging.getLogger("main")


async def _auto_init_finmind_clone() -> None:
    """Probe the FinMind clone DB; if reachable, run alembic upgrade
    head + seed dataset_sources + seed default free plan.

    Idempotent end-to-end (alembic + both seeds UPSERT semantics), so
    safe to call on every cold start. Failure is logged + swallowed —
    the FinMind subsystem is optional and the rest of the app must
    boot regardless.

    Skipped when `FINMIND_AUTO_INIT=false` (operator wants explicit
    control over migrations, e.g. multi-pod deploy where only one pod
    should migrate)."""
    from finmind.config import finmind_settings

    if not finmind_settings.FINMIND_AUTO_INIT:
        log.info("finmind auto-init disabled via FINMIND_AUTO_INIT=false")
        return

    try:
        from finmind.db.session import finmind_engine
        from finmind.scripts.init_db import (
            run_migrations,
            seed_dataset_sources,
            seed_default_free_plan,
        )
        from sqlalchemy import text as _text

        async with finmind_engine.connect() as conn:
            await conn.execute(_text("SELECT 1"))
    except Exception as exc:
        # Specifically helpful when the operator opted into Path A1
        # but didn't actually start `postgres_finmind` — the gaierror
        # / ConnectionRefusedError is just symptoms; the actionable
        # fix is "set FINMIND_USE_MAIN_DB=true" (since PR #313 the
        # default, but explicitly setting it false reverts to the
        # separate-container path).
        is_path_a1 = not finmind_settings.FINMIND_USE_MAIN_DB
        hint = (
            "Either start the `postgres_finmind` container "
            "(`docker compose --profile finmind up -d postgres_finmind`) "
            "or set FINMIND_USE_MAIN_DB=true to share the main DB."
            if is_path_a1
            else "Check main DATABASE_URL connectivity."
        )
        log.warning(
            "finmind auto-init skipped — DB not reachable (%s: %s). %s",
            exc.__class__.__name__,
            exc,
            hint,
        )
        return

    try:
        await asyncio.to_thread(run_migrations)
        seeded, total = await seed_dataset_sources()
        free_inserted = await seed_default_free_plan()
        log.info(
            "finmind auto-init done: alembic upgraded; seeded %d catalog "
            "rows (total %d); 'free' plan %s",
            seeded,
            total,
            "inserted" if free_inserted else "already present",
        )
    except Exception:
        log.exception(
            "finmind auto-init failed during migrate/seed — leaving "
            "subsystem in whatever partial state it was in. AdminPage "
            "Setup checklist will show the gap."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────
    async with engine.connect() as conn:
        await conn.execute(sqlalchemy.text("SELECT 1"))

    if not await redis_ping():
        raise RuntimeError("Cannot connect to Redis")

    async with AsyncSessionLocal() as db:
        await seed_admin(db)

    # FinMind clone DB auto-init — best-effort. When `postgres_finmind`
    # is reachable, runs alembic + seeds catalog + seeds default free
    # plan so the operator's only manual setup step is `docker compose
    # --profile finmind up -d postgres_finmind`. When the DB is down,
    # logs a warning and proceeds — the AdminPage's Setup checklist
    # already surfaces the actionable hint.
    await _auto_init_finmind_clone()

    # Verification handle for env-var propagation. Without this line,
    # diagnosing "I set FINMIND_USE_MAIN_DB=true but it didn't take
    # effect" requires inferring from absent errors. With it, a single
    # `docker compose logs backend | grep "finmind config:"` prints
    # exactly which mode + URL the engine actually bound to.
    from finmind.config import finmind_settings as _fm_settings
    log.info(
        "finmind config: USE_MAIN_DB=%s, effective_url=%s, schema=%s, "
        "AUTO_INIT=%s",
        _fm_settings.FINMIND_USE_MAIN_DB,
        _fm_settings.effective_database_url_safe,
        _fm_settings.schema,
        _fm_settings.FINMIND_AUTO_INIT,
    )

    from api.websocket.manager import publish_alert_to_user, publish_update, start_pubsub_listener
    from services.notification_service import register_push_impl, register_transport
    from services.web_push_service import push_to_user as web_push_to_user

    # Alert push goes through Redis pub/sub in every topology: the fire
    # site (often the scheduler process) publishes once, and each web
    # worker's listener delivers to the sockets it owns. This also fixes
    # the old multi-worker gap where an alert fired on worker A never
    # reached a tab connected to worker B.
    register_push_impl(publish_alert_to_user)
    # Web Push (PR-D3): browser notifications even with no tab open.
    # No-ops silently until VAPID keys are configured.
    register_transport("web_push", web_push_to_user)
    await start_pubsub_listener()

    # ── Scheduler + Kraken pump (per-topology) ───────────────────
    # Compose runs a dedicated `scheduler` container (`python worker.py`)
    # and sets SCHEDULER_ENABLED=false here so N web workers don't run
    # N copies of every job. Default true = single-process dev mode.
    crypto_pump = None
    if settings.SCHEDULER_ENABLED:
        from tasks.scheduler import scheduler, setup_jobs
        from data.crypto.kraken_ws import KrakenTickerPump

        setup_jobs()
        scheduler.start()

        # Kraken WebSocket pump — sub-second crypto ticker stream into
        # the internal pub/sub. Replaces the 30s scheduler poll for
        # symbols clients are actively subscribed to (the scheduler poll
        # stays as a safety net in case the pump is disconnected).
        crypto_pump = KrakenTickerPump(publish_update)
        crypto_pump.start()
        app.state.crypto_pump = crypto_pump

    # Warmups stay in EVERY web worker regardless of topology: they
    # populate per-process module caches (TW symbol map, S&P 500 list)
    # that request handlers in THIS process read.
    # Kick off the TW symbol-map refresh immediately. The scheduler also
    # runs it daily, but APScheduler's IntervalTrigger only fires after one
    # full interval has elapsed, so without this the search endpoint and
    # exchange lookup would return empty for the first 24 hours after a
    # cold start.
    from services.tw_market_service import (
        load_symbol_map_from_cache,
        load_symbol_map_from_db,
        refresh_symbol_map,
    )
    from tasks.tw_etf_yields_refresh import warmup_tw_etf_yields

    async def _tw_warmup() -> None:
        # Symbol map first — the ETF yield refresh iterates _exchange_map.
        # Three-tier cold-start chain so chip-name / search / industry
        # endpoints work the instant the app accepts traffic:
        #   Redis snapshot (fastest, shared across pods)
        #     → DB seed (deploy-survival when Redis is empty)
        #       → TWSE upstream (fresh data; may be blocked or slow).
        # The DB tier only runs when Redis didn't populate the maps,
        # so a happy-path warm cache short-circuits the extra query.
        if not await load_symbol_map_from_cache():
            await load_symbol_map_from_db()
        await refresh_symbol_map()
        await warmup_tw_etf_yields()

    asyncio.create_task(_tw_warmup())

    # Pre-warm the S&P 500 ticker list. Without this the first /us/screener
    # or /us/search request after a cold start either pays the Wikipedia
    # scrape latency or — if Wikipedia is unreachable — collapses to []
    # until the daily scheduler job runs.
    from data.us.sp500_universe import get_sp500_tickers
    asyncio.create_task(get_sp500_tickers())

    yield

    # ── Shutdown ─────────────────────────────────────────────────
    if crypto_pump is not None:
        await crypto_pump.stop()
    if settings.SCHEDULER_ENABLED:
        from tasks.scheduler import scheduler
        scheduler.shutdown(wait=False)
    await engine.dispose()


app = FastAPI(
    title="Fincept Web Terminal",
    description="Professional financial intelligence platform — server edition",
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(PrometheusMiddleware)

# Weak ETags on the screener/history allowlist (see middleware/etag.py).
# Registered before Prometheus in the stack (added after = runs inner)
# so 304s are still counted per-path by the metrics middleware.
app.add_middleware(ETagMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────
app.include_router(auth_router, prefix="/api/auth", tags=["Auth"])
app.include_router(us_router, prefix="/api/us", tags=["US Market"])
app.include_router(tw_router, prefix="/api/tw", tags=["TW Market"])
app.include_router(crypto_router, prefix="/api/crypto", tags=["Crypto Market"])
app.include_router(ws_router)
app.include_router(portfolio_router, prefix="/api/portfolio", tags=["Portfolio"])
app.include_router(analytics_router, prefix="/api/analytics", tags=["Analytics"])
app.include_router(ai_router, prefix="/api/ai", tags=["AI Agents"])
# B1 個股 AI 研究報告 — same /api/ai namespace, separate module so the
# chat router stays focused. Shares the daily AI quota helpers.
app.include_router(stock_report_router, prefix="/api/ai", tags=["AI Agents"])
app.include_router(watchlist_router, prefix="/api/watchlist", tags=["Watchlist"])
app.include_router(alerts_router, prefix="/api/alerts", tags=["Alerts"])
app.include_router(admin_router, prefix="/api/admin", tags=["Admin"])
app.include_router(discussion_router, prefix="/api/discussion", tags=["Discussion"])
app.include_router(global_router, prefix="/api/global", tags=["Global Market"])
app.include_router(announcements_router, prefix="/api/announcements", tags=["Announcements"])
app.include_router(system_router, prefix="/api/system", tags=["System"])
app.include_router(notifications_router, prefix="/api/notifications", tags=["Notifications"])
app.include_router(finmind_router, prefix="/api/finmind", tags=["FinMind Clone"])


def _client_is_allowed(request: Request) -> bool:
    """Allow scraping from configured CIDRs OR a valid bearer token."""
    if settings.METRICS_AUTH_TOKEN:
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer ") and header[7:] == settings.METRICS_AUTH_TOKEN:
            return True
    client_host = request.client.host if request.client else ""
    if not client_host:
        return False
    try:
        addr = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    for cidr in settings.metrics_allow_cidrs:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request) -> Response:
    if not _client_is_allowed(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="metrics access denied")
    return await metrics_endpoint(request)


@app.get("/api/health", tags=["System"])
async def health():
    redis_ok = await redis_ping()
    return {
        "status": "ok",
        "redis": "ok" if redis_ok else "error",
        "version": __version__,
    }
