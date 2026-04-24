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
│   │   ├── admin/        # User management, system stats (admin role only)
│   │   ├── auth/         # JWT login/register/refresh/logout, API keys
│   │   ├── us_market/    # US quotes, history, fundamentals, options, macro, news, search
│   │   ├── tw_market/    # TW quotes, history, institutional, margin, revenue, news
│   │   ├── portfolio/    # Holdings, transactions, P&L, optimizer, performance snapshots
│   │   ├── analytics/    # DCF, VaR, backtest
│   │   ├── ai_agents/    # SSE streaming chat (6 CFA personas, 4 LLM providers)
│   │   ├── watchlist/    # Multi-watchlist CRUD with live quote enrichment
│   │   ├── alerts/       # Price alert CRUD + check-and-fire
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
│   │                     #   Transaction, PortfolioSnapshot, Watchlist,
│   │                     #   WatchlistItem, PriceAlert
│   ├── services/         # Business logic (cached, waterfall)
│   │   ├── alert_service.py
│   │   ├── analytics_service.py
│   │   ├── portfolio_service.py
│   │   ├── tw_market_service.py
│   │   ├── us_market_service.py
│   │   └── watchlist_service.py
│   ├── tasks/            # APScheduler jobs (US 10s, TW 60s, off-hours throttle)
│   ├── tests/            # pytest — in-memory SQLite + AsyncMock Redis
│   │   ├── test_admin_api.py       # 11 tests: stats, user list, role/active CRUD
│   │   ├── test_alert_service.py   # 19 unit tests: CRUD + check_and_fire (pure)
│   │   ├── test_alerts_api.py      # 8 tests: CRUD, check-and-fire logic
│   │   ├── test_analytics.py       # 25 unit tests: DCF, VaR, backtest (pure)
│   │   ├── test_analytics_api.py   # 15 tests: DCF/VaR/backtest HTTP endpoints
│   │   ├── test_analytics_service.py # 17 unit tests: DCF/VaR/backtest orchestration
│   │   ├── test_auth_api.py        # 14 tests: register, login, refresh, API keys
│   │   ├── test_portfolio_api.py   # 12 tests: portfolio + transaction CRUD
│   │   ├── test_portfolio_extended.py # 14 tests: detail, performance, optimiser
│   │   ├── test_portfolio_optimizer.py # 16 unit tests: mean-variance + frontier (pure)
│   │   ├── test_portfolio_service_fx.py # 9 unit tests: FX cache + fallback (pure)
│   │   ├── test_tw_market_api.py   # 25 tests: all TW market endpoints
│   │   ├── test_us_market_api.py   # 21 tests: all US market endpoints
│   │   ├── test_watchlist_api.py   # 7 tests: watchlist + item CRUD
│   │   ├── test_watchlist_service.py # 16 unit tests: CRUD + enrichment (pure)
│   │   └── test_websocket_manager.py # 15 tests: auth, delta suppression, alert fan-out
│   ├── limiter.py        # slowapi Limiter (rate limiting on auth endpoints)
│   ├── logging_config.py # JSON logging (prod) / plain text (debug)
│   ├── config.py         # Pydantic Settings (env-driven)
│   ├── dependencies.py   # get_current_user (JWT + API key dual auth)
│   ├── pyproject.toml    # ruff lint config ([tool.ruff.lint] section)
│   └── main.py           # FastAPI app — all imports at top, middleware, routers, lifespan
├── frontend/
│   ├── public/           # PWA: manifest.webmanifest, sw.js, icons
│   └── src/
│       ├── components/
│       │   ├── charts/   # CandlestickChart (lightweight-charts v4, theme-aware)
│       │   ├── layout/   # AppLayout (top bar + sidebar), Sidebar, NotificationBell
│       │   └── portfolio/ # AllocationPie, HoldingsTable
│       ├── hooks/
│       │   ├── useWebSocket.ts       # Singleton WS + useAlertSocket() hook
│       │   ├── useWebSocket.test.ts  # 11 tests: connect, routing, alert, cleanup
│       │   ├── usePortfolio.ts       # Portfolio CRUD + optimise mutations
│       │   └── usePortfolio.test.ts  # 8 tests: query keys, enabled-gate, invalidation
│       ├── pages/        # One file per route (13 pages)
│       │   # AIPage, AdminPage, AlertsPage, AnalyticsPage, DashboardPage,
│       │   # LoginPage, MacroPage, MarketPage, PortfolioPage, ScreenerPage,
│       │   # SettingsPage, StockDetailPage, WatchlistPage
│       ├── store/        # Zustand: authStore (+ .test), notificationStore, themeStore
│       ├── test/         # Vitest setup (jsdom + RTL cleanup)
│       ├── types/        # TypeScript interfaces (market, portfolio, analytics)
│       └── lib/          # api.ts (+ .test: bearer/refresh/dedup), auth.ts (silentRefresh)
├── helm/fincept-web/     # Kubernetes Helm chart
├── docker/               # nginx.conf, redis.conf
├── .github/workflows/    # ci.yml (pytest + ruff + tsc + eslint + build)
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
- Zustand for auth + notifications + theme
- PWA: manifest + service worker (cache-first static, network-first /api/)

## Running tests

```bash
# Backend
cd backend
pytest tests/ -v --asyncio-mode=auto

# Frontend
cd frontend
npm test            # one-shot
npm run test:watch  # watch mode
```

Backend tests use in-memory SQLite (aiosqlite) and AsyncMock Redis — no external services needed.
Frontend tests use Vitest + jsdom; see `src/test/setup.ts` for global setup.

**Note:** The `client`-fixture tests (`test_auth_api`, `test_portfolio_api`, etc.) produce
`pyo3_runtime.PanicException` errors in this dev environment due to a missing `_cffi_backend`
compiled extension (`ModuleNotFoundError: No module named '_cffi_backend'`). This is a
system-level environment issue, not a code defect. All tests pass in CI (ubuntu-latest with a
full Python install).

Locally-runnable pure unit tests (no jose/cryptography dependency): 102 tests across
`test_analytics.py`, `test_analytics_service.py`, `test_portfolio_optimizer.py`,
`test_portfolio_service_fx.py`, `test_watchlist_service.py`, `test_alert_service.py`.

## Running lint / type-check

```bash
# Backend
cd backend
ruff check . --select E,W,F --ignore E501   # zero warnings expected

# Frontend
cd frontend
npx tsc --noEmit                            # zero errors expected
npm run lint                                # ESLint v9 flat config, zero warnings
```

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
Services: nginx (80/443) → frontend (Vite build) + backend (uvicorn, 2 workers) + postgresql + redis

**Kubernetes** (Helm):
```bash
helm install fincept ./helm/fincept-web \
  --set postgresql.password=<pg-pass> \
  --set redis.password=<redis-pass> \
  --set env.backend.JWT_SECRET_KEY=<secret> \
  --set ingress.host=fincept.example.com
```

## Conventions

### General
- All backend timestamps: UTC, stored as `DateTime(timezone=True)`
- All UUIDs: `uuid.UUID` Python type, `UUID(as_uuid=True)` SQLAlchemy column
- TW ROC calendar offset: `int(year_str) + 1911`
- TWSE rate limit: `asyncio.Semaphore(1)` + 1.1s delay between requests
- No comments explaining WHAT code does; only WHY when non-obvious
- Never use lightweight-charts v5 API in this project

### Backend lint (ruff)
- Config in `backend/pyproject.toml` under `[tool.ruff.lint]` (not `[tool.ruff]`)
- `type PriceCache = dict[str, float]` is Python 3.12+ syntax — use plain assignment instead:
  `PriceCache = dict[str, float]`
- `F821` on `Mapped["User"]` string annotations → fix with `TYPE_CHECKING` guard

### Frontend API calls
- `api.ts` sets `baseURL: "/api"` — **never** include `/api` prefix in path arguments.
  Use `api.get("/auth/me")`, not `api.get("/api/auth/me")`.
- The two exceptions are raw `axios.post("/api/auth/refresh", ...)` in `auth.ts`
  (uses base axios without the instance) and `fetch("/api/ai/chat", ...)` in `AIPage.tsx`
  (uses native fetch, not the axios instance).

### Frontend theme-aware chart colors
- Recharts components: use CSS variables via string literals, e.g.
  `stroke="hsl(var(--border))"`, `fill: "hsl(var(--muted-foreground))"`.
- lightweight-charts (CandlestickChart): CSS variables cannot be passed directly.
  Use `getComputedStyle(document.documentElement).getPropertyValue("--border")` and
  wrap with `hsl(...)`. Subscribe to `useThemeStore` and call `chart.applyOptions()`
  in a `useEffect([theme])` to re-apply colors on toggle without recreating the chart.
- Data-series colors (`#22c55e` green, `#ef4444` red, `#3b82f6` blue, etc.) are
  intentional semantic colors — do NOT replace with CSS variables.

### Frontend ESLint (v9 flat config)
- Config file: `frontend/eslint.config.js` using `typescript-eslint` unified package
- `@typescript-eslint/no-explicit-any`: off
- `react-hooks/incompatible-library`: off (TanStack Virtual not React Compiler aware)
- "Latest callback" ref pattern (`cbRef.current = callback` during render) is valid;
  suppress with `// eslint-disable-next-line react-hooks/refs` if flagged.
- Sub-components defined inside a parent component body are flagged by
  `react-hooks/static-components` — move them outside with explicit props.

### @types/react 18.3 JSX pitfalls
- `{/* comment */}` block comments in JSX produce `void` which is not in `ReactNode` —
  this causes the next sibling expression to show a cascade type error. Remove all
  top-level JSX block comments from component return statements.
- `{condition && <expr>}` where `condition` has type `unknown` produces `unknown` —
  wrap with `Boolean(condition)` or use a ternary.
- Optional chaining: `h.current_price?.toFixed(2)` on `number | null | undefined` —
  guard with `(h.current_price ?? 0).toFixed(2)`.
