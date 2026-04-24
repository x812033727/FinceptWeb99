# Fincept Web Terminal — CLAUDE.md

Professional financial intelligence platform: FastAPI backend + React frontend,
mirroring the FinceptTerminal C++/Qt6 desktop app as a server-side web service.

## Quick start

```bash
# Backend (Python 3.11+)
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env          # fill in API keys
alembic upgrade head
uvicorn main:app --reload

# Frontend (Node 20+)
cd frontend
npm install
npm run dev                   # Vite dev server → http://localhost:5173
```

Docker Compose (full stack):
```bash
docker compose up --build
```

## Repo structure

```
FinceptWeb/
├── backend/
│   ├── api/              # FastAPI routers, one package per domain
│   │   ├── auth/         # JWT login/register/refresh/logout, API keys
│   │   ├── us_market/    # US quotes, history, fundamentals, options, macro, news
│   │   ├── tw_market/    # TW quotes, history, institutional, margin, revenue, news
│   │   ├── portfolio/    # Holdings, transactions, P&L, optimizer
│   │   ├── analytics/    # DCF, VaR, backtest
│   │   ├── ai_agents/    # SSE streaming chat (6 CFA personas, 4 LLM providers)
│   │   ├── watchlist/    # Multi-watchlist CRUD with live quote enrichment
│   │   ├── alerts/       # Price alert CRUD
│   │   └── websocket/    # Auth-first WS, Redis pub/sub, delta suppression
│   ├── ai/               # LLM router + agent persona definitions
│   ├── analytics/        # Pure computation: dcf.py, risk.py, backtest.py
│   ├── auth/             # JWT handler + role permissions
│   ├── cache/            # Redis helpers (get/set/delete, key helpers)
│   ├── data/
│   │   ├── us/           # Polygon → yfinance waterfall; FRED connector
│   │   └── tw/           # TWSE → FinMind → MOPS waterfall
│   ├── db/
│   │   ├── migrations/   # Alembic versions (0001 initial, 0002 price_alerts)
│   │   ├── base.py       # DeclarativeBase with naming convention
│   │   ├── seed.py       # Admin user seed on first boot
│   │   └── session.py    # Async engine + get_db dependency
│   ├── middleware/
│   │   └── metrics.py    # Prometheus middleware + /metrics endpoint
│   ├── models/           # SQLAlchemy ORM: User, APIKey, Portfolio, Holding,
│   │                     #   Transaction, Watchlist, WatchlistItem, PriceAlert
│   ├── services/         # Business logic (cached, waterfall)
│   ├── tasks/            # APScheduler jobs (US 10s, TW 60s, off-hours throttle)
│   ├── tests/            # pytest with in-memory SQLite + AsyncMock Redis
│   ├── limiter.py        # slowapi Limiter (rate limiting on auth endpoints)
│   ├── logging_config.py # JSON logging (prod) / plain text (debug)
│   ├── config.py         # Pydantic Settings (env-driven)
│   ├── dependencies.py   # get_current_user (JWT + API key dual auth)
│   └── main.py           # FastAPI app, middleware, routers, lifespan
├── frontend/
│   ├── public/           # PWA: manifest.webmanifest, sw.js, icons
│   └── src/
│       ├── components/
│       │   ├── charts/   # CandlestickChart (lightweight-charts v4)
│       │   ├── layout/   # AppLayout (top bar + sidebar), Sidebar, NotificationBell
│       │   └── portfolio/ # AllocationPie, HoldingsTable
│       ├── hooks/
│       │   ├── useWebSocket.ts   # Singleton WS + useAlertSocket() hook
│       │   └── usePortfolio.ts
│       ├── pages/        # One file per route
│       ├── store/        # Zustand: authStore, notificationStore
│       ├── types/        # TypeScript interfaces (market, portfolio, analytics)
│       └── lib/          # api.ts (axios), auth.ts (silentRefresh)
├── helm/fincept-web/     # Kubernetes Helm chart
├── docker/               # nginx.conf, redis.conf
├── .github/workflows/    # ci.yml (pytest + lint + build), docker.yml (GHCR push)
├── docker-compose.yml
└── CLAUDE.md
```

## Key architectural decisions

### Auth
- Access token: 15-min JWT in memory (Zustand store)
- Refresh token: 7-day JWT in httpOnly cookie, per-jti Redis revocation
- Dual auth: Bearer JWT **or** `X-API-Key` header (sha256-hashed in DB)
- Roles: `viewer` → `analyst` → `admin`; permissions in `auth/permissions.py`

### Caching (3-tier)
1. In-process (module-level dicts for symbol maps, S&P 500 list)
2. Redis (quotes 15s, history 4h, fundamentals 24h, screener 10m, news 5m)
3. PostgreSQL (persistent holdings, transactions, watchlists, alerts)

### Market data waterfall
- US: Polygon.io → yfinance (quote, history, fundamentals, options, news)
- TW: TWSE OpenAPI → FinMind → MOPS; BWIBBU_d for PE/PB/yield
- Macro: FRED API (fed_funds_rate, cpi, gdp, yield curve, USD index, TWD/USD)

### WebSocket
- Auth-first: client sends `{"action":"auth","token":"..."}` within 5s
- Redis pub/sub fan-out; delta suppression (< 0.01% change skipped)
- 30s heartbeat (ping/pong); per-user connection map for alert push

### Analytics (heavy compute)
- DCF: sensitivity grid 5×3 (WACC × growth), bull/base/bear scenarios
- VaR: historical / parametric / Monte Carlo (Cholesky correlation)
- Backtest: SMA crossover + RSI strategies, event-driven
- All heavy paths run in `ProcessPoolExecutor(max_workers=2)` with 30s timeout

### AI agents
- 6 CFA personas: equity_analyst, risk_manager, portfolio_advisor,
  macro_analyst, options_strategist, quant_researcher
- LLM router: OpenAI / Anthropic / Gemini / Ollama
- SSE streaming via FastAPI `StreamingResponse`
- Redis daily quota (viewer: 5 req/day, analyst: 20 req/day)

### Frontend
- React 18 + TypeScript + Vite; TanStack Query (staleTime 15s)
- lightweight-charts **v4** API (`chart.addCandlestickSeries()` — NOT v5)
- TanStack Virtual for screener rows
- Zustand for auth + notifications
- PWA: manifest + service worker (cache-first static, network-first /api/)

## Running tests

```bash
cd backend
pytest tests/ -v --asyncio-mode=auto
```

Tests use in-memory SQLite (aiosqlite) and AsyncMock Redis — no external services needed.

## Environment variables (backend `.env`)

```
DATABASE_URL=postgresql+asyncpg://fincept:password@localhost:5432/finceptweb
REDIS_URL=redis://localhost:6379
JWT_SECRET_KEY=<min-32-char-secret>
POLYGON_API_KEY=          # optional — falls back to yfinance
FRED_API_KEY=             # optional — macro data
FINMIND_TOKEN=            # optional — TW institutional data
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
OLLAMA_HOST=http://localhost:11434
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=<password>
DEBUG=false
CORS_ORIGINS=http://localhost:5173
```

## Migrations

```bash
cd backend
alembic upgrade head          # apply all migrations
alembic revision -m "description" --autogenerate   # generate new migration
```

## Deployment

**Docker Compose** (single server):
```bash
docker compose up -d
```
Services: nginx (80/443) → frontend (Vite build) + backend (uvicorn, 2 workers) + timescaledb + redis

**Kubernetes** (Helm):
```bash
helm install fincept ./helm/fincept-web \
  --set postgresql.password=<pg-pass> \
  --set redis.password=<redis-pass> \
  --set env.backend.JWT_SECRET_KEY=<secret> \
  --set ingress.host=fincept.example.com
```

## Conventions

- All backend timestamps: UTC, stored as `DateTime(timezone=True)`
- All UUIDs: `uuid.UUID` Python type, `UUID(as_uuid=True)` SQLAlchemy column
- TW ROC calendar offset: `int(year_str) + 1911`
- TWSE rate limit: `asyncio.Semaphore(1)` + 1.1s delay between requests
- No comments explaining WHAT code does; only WHY when non-obvious
- Never use lightweight-charts v5 API in this project
