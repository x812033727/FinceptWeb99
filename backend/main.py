from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import sqlalchemy

from config import settings
from cache.redis_cache import ping as redis_ping
from db.session import engine, AsyncSessionLocal
from db.seed import seed_admin


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────
    async with engine.connect() as conn:
        await conn.execute(sqlalchemy.text("SELECT 1"))

    if not await redis_ping():
        raise RuntimeError("Cannot connect to Redis")

    async with AsyncSessionLocal() as db:
        await seed_admin(db)

    # TODO Phase 5: start APScheduler
    # TODO Phase 5: start Redis Pub/Sub listener

    yield

    # ── Shutdown ─────────────────────────────────────────────────
    await engine.dispose()


app = FastAPI(
    title="Fincept Web Terminal",
    description="Professional financial intelligence platform — server edition",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────
from api.auth.router import router as auth_router
app.include_router(auth_router, prefix="/api/auth", tags=["Auth"])

# Phase 3:  from api.us_market.router import router as us_router
#           app.include_router(us_router, prefix="/api/us", tags=["US Market"])
# Phase 4:  from api.tw_market.router import router as tw_router
#           app.include_router(tw_router, prefix="/api/tw", tags=["TW Market"])
# Phase 5:  from api.websocket.router import router as ws_router
#           app.include_router(ws_router)
# Phase 6:  from api.portfolio.router import router as portfolio_router
#           app.include_router(portfolio_router, prefix="/api/portfolio", tags=["Portfolio"])
# Phase 7:  from api.analytics.router import router as analytics_router
#           app.include_router(analytics_router, prefix="/api/analytics", tags=["Analytics"])
# Phase 8:  from api.ai_agents.router import router as ai_router
#           app.include_router(ai_router, prefix="/api/ai", tags=["AI Agents"])


@app.get("/api/health", tags=["System"])
async def health():
    redis_ok = await redis_ping()
    return {
        "status": "ok",
        "redis": "ok" if redis_ok else "error",
        "version": "0.1.0",
    }
