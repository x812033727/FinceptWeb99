import ipaddress
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
from api.alerts.router import router as alerts_router
from api.analytics.router import router as analytics_router
from api.auth.router import router as auth_router
from api.crypto_market.router import router as crypto_router
from api.portfolio.router import router as portfolio_router
from api.system.router import router as system_router
from api.tw_market.router import router as tw_router
from api.us_market.router import router as us_router
from api.watchlist.router import router as watchlist_router
from api.websocket.router import router as ws_router
from cache.redis_cache import ping as redis_ping
from config import settings
from db.seed import seed_admin
from db.session import AsyncSessionLocal, engine
from limiter import limiter
from logging_config import setup_logging
from middleware.metrics import PrometheusMiddleware, metrics_endpoint

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────
    async with engine.connect() as conn:
        await conn.execute(sqlalchemy.text("SELECT 1"))

    if not await redis_ping():
        raise RuntimeError("Cannot connect to Redis")

    async with AsyncSessionLocal() as db:
        await seed_admin(db)

    from tasks.scheduler import scheduler, setup_jobs
    from api.websocket.manager import push_alert_to_user, publish_update, start_pubsub_listener
    from services.notification_service import register_push_impl

    register_push_impl(push_alert_to_user)
    setup_jobs()
    scheduler.start()
    await start_pubsub_listener()

    # Kraken WebSocket pump — sub-second crypto ticker stream into the
    # internal pub/sub. Replaces the 30s scheduler poll for symbols clients
    # are actively subscribed to (the scheduler poll stays as a safety net
    # in case the pump is disconnected).
    from data.crypto.kraken_ws import KrakenTickerPump
    crypto_pump = KrakenTickerPump(publish_update)
    crypto_pump.start()
    app.state.crypto_pump = crypto_pump

    yield

    # ── Shutdown ─────────────────────────────────────────────────
    await crypto_pump.stop()
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
app.include_router(watchlist_router, prefix="/api/watchlist", tags=["Watchlist"])
app.include_router(alerts_router, prefix="/api/alerts", tags=["Alerts"])
app.include_router(admin_router, prefix="/api/admin", tags=["Admin"])
app.include_router(system_router, prefix="/api/system", tags=["System"])


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
