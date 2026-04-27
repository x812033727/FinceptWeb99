# Fincept Web Terminal — CLAUDE.md

Professional financial intelligence platform: FastAPI backend + React frontend,
mirroring the FinceptTerminal C++/Qt6 desktop app as a server-side web service.

> **Mainline branch:** `claude/create-terminal-documentation-iBmfW`.
> All PRs target this branch (not `main`). The deployed backend / frontend
> reads from here, so for changes to take effect you must (1) merge the PR
> into this branch and (2) restart uvicorn / redeploy the container —
> Python doesn't hot-reload the data-source connector layer.

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
│   │   ├── crypto_market/ # Kraken-backed Top 20 crypto: quote, history, screener, search
│   │   ├── portfolio/    # Holdings, transactions, P&L, optimizer, performance snapshots
│   │   ├── analytics/    # DCF, VaR, backtest
│   │   ├── ai_agents/    # SSE streaming chat (19 personas, 8 LLM providers)
│   │   ├── watchlist/    # Multi-watchlist CRUD with live quote enrichment
│   │   ├── alerts/       # Price alert CRUD + check-and-fire
│   │   ├── system/       # /version (GitHub latest) + /web-vital (Core Web Vitals → Prometheus)
│   │   └── websocket/    # Auth-first WS, Redis pub/sub, delta suppression
│   ├── ai/               # LLM router + agent persona definitions
│   ├── analytics/        # Pure computation: dcf.py, risk.py, backtest.py
│   ├── auth/             # JWT handler + role permissions
│   ├── cache/            # Redis helpers (get/set/delete, key helpers)
│   ├── data/
│   │   ├── us/           # Polygon → yfinance → Stooq waterfall; FRED macro
│   │   ├── tw/           # TWSE → FinMind → MOPS waterfall
│   │   └── crypto/       # Kraken REST connector + WS pump (kraken_ws.py) + Top 20 universe (symbols.py)
│   ├── db/
│   │   ├── migrations/   # Alembic versions:
│   │   │                 #   0001 initial · 0002 price_alerts ·
│   │   │                 #   0003 portfolio_snapshots · 0004 → hypertable ·
│   │   │                 #   0005 llm_provider_keys · 0006 persona_overrides ·
│   │   │                 #   0007 user_llm_provider_keys · 0008 llm_usage_events ·
│   │   │                 #   0009 crypto_market
│   │   ├── base.py       # DeclarativeBase with naming convention
│   │   ├── seed.py       # Admin user seed on first boot
│   │   └── session.py    # Async engine + get_db dependency
│   ├── middleware/
│   │   └── metrics.py    # Prometheus middleware + /metrics endpoint
│   ├── models/           # SQLAlchemy ORM: User, APIKey, Portfolio, Holding,
│   │                     #   Transaction, PortfolioSnapshot, Watchlist,
│   │                     #   WatchlistItem, PriceAlert, LLMProviderKey,
│   │                     #   UserLLMProviderKey, PersonaOverride, LLMUsageEvent
│   ├── services/         # Business logic (cached, waterfall)
│   │   ├── alert_service.py             # Price alert CRUD + check_and_fire
│   │   ├── analytics_service.py         # DCF/VaR/backtest orchestration (ProcessPool)
│   │   ├── crypto_market_service.py     # Kraken quote/history/screener (24/7)
│   │   ├── llm_key_service.py           # DB-first LLM provider key mgmt (Fernet at rest)
│   │   ├── llm_usage_service.py         # Token + cost tracking (RATE_TABLE)
│   │   ├── notification_service.py      # Decoupled push dispatcher (WS-registered)
│   │   ├── persona_override_service.py  # Per-persona LLM provider/model overrides
│   │   ├── portfolio_service.py         # CRUD, P&L, multi-currency FX cache, optimiser
│   │   ├── tw_market_service.py         # TW: TWSE → FinMind → MOPS; don't-cache-empty
│   │   ├── us_market_service.py         # US: Polygon → yfinance → Stooq waterfall
│   │   ├── version_service.py           # GitHub release polling + admin-triggered update
│   │   └── watchlist_service.py         # CRUD + live quote enrichment
│   ├── tasks/            # APScheduler jobs (US 10s, TW 60s, off-hours throttle)
│   ├── tests/            # pytest — in-memory SQLite + AsyncMock Redis
│   │                     # 48 files, ~600 tests. Categories:
│   │                     #   API HTTP   : test_*_api.py        (admin, auth, alerts,
│   │                     #                                      analytics, portfolio,
│   │                     #                                      tw_market 42, us_market 23,
│   │                     #                                      watchlist)
│   │                     #   Services   : test_*_service*.py   (alert, analytics,
│   │                     #                                      crypto_market, portfolio,
│   │                     #                                      tw_market_caching,
│   │                     #                                      us_market, watchlist,
│   │                     #                                      llm_key, llm_usage,
│   │                     #                                      tw_health, version)
│   │                     #   Connectors : test_*_connector.py  (yfinance, polygon, stooq,
│   │                     #                                      fred, twse, finmind, mops,
│   │                     #                                      kraken, kraken_ws)
│   │                     #   AI / chat  : test_ai_agents, test_ai_quota_refund,
│   │                     #                test_ai_tools, test_openai_compat_providers,
│   │                     #                test_claude_agent_config, test_claude_agent_chat
│   │                     #   Misc       : test_websocket_manager, test_web_vital_endpoint,
│   │                     #                test_portfolio_optimizer, test_portfolio_service_fx
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
│       │   ├── layout/   # AppLayout, Sidebar (+ .test), UpdateBadge, NotificationBell
│       │   ├── portfolio/ # AllocationPie, HoldingsTable (+ .test)
│       │   └── (root)    # ErrorBoundary, Skeleton, Toaster (each + .test)
│       ├── hooks/
│       │   ├── useWebSocket.ts       # Singleton WS + useAlertSocket() hook
│       │   ├── useWebSocket.test.ts  # 11 tests: connect, routing, alert, cleanup
│       │   ├── usePortfolio.ts       # Portfolio CRUD + optimise mutations
│       │   ├── usePortfolio.test.ts  # 8 tests: query keys, enabled-gate, invalidation
│       │   └── useVersion.ts         # /api/system/version polling for UpdateBadge
│       ├── pages/        # One file per route (13 pages)
│       │   # AIPage, AdminPage, AlertsPage, AnalyticsPage, DashboardPage,
│       │   # LoginPage, MacroPage, MarketPage, PortfolioPage, ScreenerPage,
│       │   # SettingsPage, StockDetailPage, WatchlistPage
│       ├── store/        # Zustand. Each *Store.ts pairs with *Store.test.ts:
│       │                 #   authStore, notificationStore, themeStore, toastStore
│       │                 # Plain modules (no test): analytics, market, portfolio, system
│       ├── test/         # Vitest setup (jsdom + RTL cleanup)
│       ├── types/        # TypeScript interfaces (market, portfolio, analytics, system)
│       └── lib/          # api.ts (+ .test: bearer/refresh/dedup), auth.ts (silentRefresh),
│                         # webVitals.ts (+ .test) — Core Web Vitals → POST /api/system/web-vital
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
- US: Polygon.io → yfinance → Stooq (quote, history, fundamentals, news).
  Options chain is `Polygon → yfinance` only (Stooq has no options endpoint);
  free-tier deployments without a Polygon key still serve chains via
  `yfinance.get_options()` which exposes last_price / IV / OI per contract.
- US screener fallback chain: Polygon snapshot → `_screener_yfinance` (per-
  symbol `.info`, slow + frequently rate-limited) → curated `_FALLBACK_UNIVERSE`
  enriched via `yfinance.get_batch_quotes()` (`yf.download()` chart endpoint,
  more resilient than `.info`) → `stooq.get_batch_quotes()` (free CSV API,
  Polish edge, immune to Yahoo's cloud-IP block). Rows where every price is
  0 are NOT cached so the next request retries instead of locking in 10 min
  of zeros.
- Stooq is single-symbol-only (its comma-batch endpoint trips a "DNS
  cache overflow" 503 anti-scrape) and rejects parallel calls (even 2 in
  flight = 503), so `get_batch_quotes` walks the symbol list sequentially
  with a 0.2s inter-request delay. ~5 syms/sec. Free, no API key. Field
  set `f=sd2t2ohlcvp` returns `Prev` (previous close) so we get change%
  without a separate history call. The history endpoint (`q/d/l/`) is
  captcha-gated behind an apikey and not used.
- TW: TWSE OpenAPI → FinMind → MOPS; BWIBBU_d for PE/PB/yield. Empty
  results (both upstream sources failed) are NOT cached — mirrors the US
  service so a transient TWSE+FinMind failure doesn't lock 60 s of zero
  state. Applies to quote / history / institutional / margin / revenue /
  fundamentals.
- Crypto: Kraken public REST (`get_quote`/`get_history`); Top 20 universe in
  `data/crypto/symbols.py`. USDT/USDC/DAI normalized to USD for FX
  (`_normalize_currency` in `portfolio_service.py`)
- Macro: FRED API (fed_funds_rate, cpi, gdp, yield curve, USD index, TWD/USD)

### WebSocket
- Auth-first: client sends `{"action":"auth","token":"..."}` within 5s
- Redis pub/sub fan-out; delta suppression (< 0.01% change skipped)
- 30s heartbeat (ping/pong); per-user connection map for alert push
- Crypto live: outbound `KrakenTickerPump` (`data/crypto/kraken_ws.py`)
  connects to `wss://ws.kraken.com/v2`, subscribes to all Top 20 ticker
  channels, forwards each tick into `publish_update` so existing client
  WS subscribers see sub-second prices. Started in `main.py` lifespan;
  reconnects with exponential backoff (1s → 2s → ... cap 60s)

### Analytics (heavy compute)
- DCF: sensitivity grid 5×3 (WACC × growth), bull/base/bear scenarios
- VaR: historical / parametric / Monte Carlo (Cholesky correlation)
- Backtest: SMA crossover + RSI strategies, event-driven
- All heavy paths run in `ProcessPoolExecutor(max_workers=2)` with 30s timeout

### AI agents
- **19 personas** across two groups:
  - 7 CFA-style functional: market_analyst, portfolio_advisor, risk_manager,
    macro_analyst, earnings_analyst, trading_coach, claude_research
  - 12 legendary investors: buffett, graham, munger (value); lynch, fisher,
    smith (quality-growth); marks, klarman (contrarian); dalio, soros (macro);
    simons, asness (quant)
- LLM router supports **8 providers**: OpenAI / Anthropic / Gemini / Ollama /
  MiniMax / Groq / DeepSeek / OpenRouter (the last 4 share an
  `_openai_compat_tool_loop` helper) + Claude Agent (tool-use via SDK)
- SSE streaming via FastAPI `StreamingResponse`
- Redis daily quota (viewer: 5 req/day, analyst: 20 req/day)
- Tool-use: `claude_agent` uses MCP via `claude-agent-sdk`; the four
  OpenAI-compat providers use a shared toolset (`build_openai_compat_toolset`)
  exposing get_quote / run_dcf / run_var / run_backtest / query_user_data —
  only analyst/admin role gets tools, viewer falls back to plain chat
- Claude Agent enable gate is `Settings.claude_agent_effective_enabled`,
  which ANDs `CLAUDE_AGENT_ENABLED` (default `True`) with importability of
  `claude_agent_sdk`. Deployments without the optional SDK degrade to a
  503 with a clear message instead of crashing on import; installing the
  SDK opts in for free without flag-flipping. The `web_fetch` tool is
  hard-gated by `CLAUDE_AGENT_WEBFETCH_ALLOWLIST` (curated default covers
  GitHub raw, Anthropic / FastAPI docs, FRED, SEC, Yahoo chart).

### Frontend
- React 18 + TypeScript + Vite; TanStack Query (staleTime 15s)
- lightweight-charts **v4** API (`chart.addCandlestickSeries()` — NOT v5)
- TanStack Virtual for screener rows
- Zustand for auth + notifications + theme + toasts
- PWA: manifest + service worker (cache-first static, network-first /api/)

### Observability
- Prometheus middleware (`middleware/metrics.py`) exposes `/metrics`,
  protected by `METRICS_ALLOW_CIDRS` (loopback default) or `METRICS_AUTH_TOKEN`.
- Core Web Vitals: `frontend/src/lib/webVitals.ts` reports CLS / LCP / FID /
  INP / FCP / TTFB to `POST /api/system/web-vital`, which records them
  as Prometheus histograms keyed by metric name + page route. Frontend
  module is bundle-split so the `web-vitals` library doesn't bloat first paint.

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

Locally-runnable pure unit tests (no jose/cryptography dependency, no `client` fixture):
mock the data connectors + cache helpers and run independently. Examples include
`test_analytics_service`, `test_portfolio_optimizer`, `test_portfolio_service_fx`,
`test_watchlist_service`, `test_alert_service`, `test_us_market_service`,
`test_tw_market_service_caching`, `test_crypto_market_service`, `test_version_service`,
`test_claude_agent_config`, plus all `test_*_connector.py` files. Each new
service-layer change should land with a matching `tests/test_*_service*.py` so the
fix is covered without needing the full DB/Redis stack.

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
GITHUB_OWNER=x812033727
GITHUB_REPO=FinceptWeb
UPDATE_CHECK_INTERVAL_HOURS=6
UPDATE_COMMAND=                 # empty = /api/admin/update returns "not_configured"
```

## Versioning

Single source of truth: `backend/_version.py::__version__`. To bump:

```bash
# 1. edit backend/_version.py
# 2. mirror to frontend/package.json
python scripts/sync-version.py

# 3. tag and push
git tag v0.2.0 && git push origin v0.2.0
```

CI `version-sync` job fails if `_version.py` and `package.json` drift.
Tag push triggers `.github/workflows/release.yml` which validates the tag
matches `__version__` and creates a GitHub Release. Backend's
`/api/system/version` reads `releases/latest` so newer versions surface as
an "update available" badge in the TopBar; admins can click "Update now"
in `AdminPage` to invoke `UPDATE_COMMAND` (typically
`docker compose pull && docker compose up -d backend frontend`).

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
- lightweight-charts (CandlestickChart): its v4 color parser ONLY accepts hex,
  named colors, and `rgb()`/`rgba()` — it rejects every form of `hsl()` (legacy
  comma form `hsl(215, 20%, 55%)` and modern space form `hsl(215 20% 55%)` both
  throw `Cannot parse color`). Since the project's CSS vars are shadcn-style
  space-separated HSL components (`215 20% 55%`), read them with
  `getComputedStyle(document.documentElement).getPropertyValue("--border")`, parse
  the H/S/L numbers, convert to RGB in JS, and pass `rgb(r, g, b)` to the chart.
  See `hslVarToRgb` in `CandlestickChart.tsx`. Subscribe to `useThemeStore` and
  call `chart.applyOptions()` in a `useEffect([theme])` to re-apply colors on
  toggle without recreating the chart.
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
