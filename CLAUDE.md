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
│   │   ├── global_market/ # International news (Fed / FOMC / global macro, market='GLOBAL')
│   │   ├── portfolio/    # Holdings, transactions, P&L, optimizer, performance snapshots
│   │   ├── analytics/    # DCF, VaR, backtest
│   │   ├── ai_agents/    # SSE streaming chat (19 personas, 8 LLM providers)
│   │   ├── watchlist/    # Multi-watchlist CRUD with live quote enrichment
│   │   ├── alerts/       # Price alert CRUD + check-and-fire
│   │   ├── discussion/   # Multi-persona round-table discussions (SSE rounds + synthesizer)
│   │   ├── system/       # /version (GitHub latest) + /web-vital (Core Web Vitals → Prometheus)
│   │   └── websocket/    # Auth-first WS, Redis pub/sub, delta suppression
│   ├── ai/               # LLM router + agent persona definitions
│   ├── analytics/        # Pure computation: dcf.py, risk.py, backtest.py
│   ├── auth/             # JWT handler + role permissions
│   ├── cache/            # Redis helpers (get/set/delete, key helpers)
│   ├── data/
│   │   ├── us/           # Polygon → yfinance → Stooq → Finnhub waterfall; FRED macro
│   │   ├── tw/           # TWSE → FinMind → MOPS waterfall
│   │   └── crypto/       # Kraken REST connector + WS pump (kraken_ws.py) + Top 20 universe (symbols.py)
│   ├── db/
│   │   ├── migrations/   # Alembic versions:
│   │   │                 #   0001 initial · 0002 price_alerts ·
│   │   │                 #   0003 portfolio_snapshots · 0004 → hypertable ·
│   │   │                 #   0005 llm_provider_keys · 0006 persona_overrides ·
│   │   │                 #   0007 user_llm_provider_keys · 0008 llm_usage_events ·
│   │   │                 #   0009 crypto_market · 0010 market_provider_keys ·
│   │   │                 #   0011 ohlcv_daily · 0012 quote_snapshots ·
│   │   │                 #   0013 fundamentals_snapshots · 0014 news_articles ·
│   │   │                 #   0015 discussions + news_articles.sentiment_* ·
│   │   │                 #   0016 system_task_configs ·
│   │   │                 #   0017 runtime_settings ·
│   │   │                 #   0018 discussion_verdict · 0019 discussion_day5_close ·
│   │   │                 #   0020 discussion_auto_run_configs ·
│   │   │                 #   0021 tw_institutional_daily + tw_margin_daily ·
│   │   │                 #   0022 tw_revenue_monthly ·
│   │   │                 #   0023 discussion_round_contexts ·
│   │   │                 #   0024 discussions.daily_close_prices
│   │   ├── base.py       # DeclarativeBase with naming convention
│   │   ├── seed.py       # Admin user seed on first boot
│   │   └── session.py    # Async engine + get_db dependency
│   ├── middleware/
│   │   └── metrics.py    # Prometheus middleware + /metrics endpoint
│   ├── models/           # SQLAlchemy ORM: User, APIKey, Portfolio, Holding,
│   │                     #   Transaction, PortfolioSnapshot, Watchlist,
│   │                     #   WatchlistItem, PriceAlert, LLMProviderKey,
│   │                     #   UserLLMProviderKey, PersonaOverride, LLMUsageEvent,
│   │                     #   MarketProviderKey, OhlcvDaily, QuoteSnapshot,
│   │                     #   FundamentalsSnapshot, NewsArticle, Discussion,
│   │                     #   DiscussionTurn, SystemTaskConfig
│   ├── services/         # Business logic (cached, waterfall)
│   │   ├── alert_service.py             # Price alert CRUD + check_and_fire
│   │   ├── analytics_service.py         # DCF/VaR/backtest orchestration (ProcessPool)
│   │   ├── crypto_market_service.py     # Kraken quote/history/screener (24/7)
│   │   ├── discussion_service.py        # Round-table orchestrator: gather context,
│   │   │                                #   run_round (SSE), synthesize_conclusion,
│   │   │                                #   batch persona resolution, try/finally state
│   │   ├── llm_key_service.py           # DB-first LLM provider key mgmt (Fernet at rest)
│   │   ├── llm_usage_service.py         # Token + cost tracking (RATE_TABLE)
│   │   ├── news_sentiment_service.py    # Hourly batch scoring + market/per-symbol
│   │   │                                #   sentiment aggregators (used by discussion ctx)
│   │   ├── notification_service.py      # Decoupled push dispatcher (WS-registered)
│   │   ├── persona_override_service.py  # Per-persona LLM provider/model overrides
│   │   ├── portfolio_service.py         # CRUD, P&L, multi-currency FX cache, optimiser
│   │   ├── system_task_config_service.py # Admin LLM routing for background tasks
│   │   │                                #   (news_sentiment, discussion_synthesizer)
│   │   ├── tw_market_service.py         # TW: TWSE → FinMind → MOPS; don't-cache-empty
│   │   ├── us_market_service.py         # US: Polygon → yfinance → Stooq → Finnhub waterfall
│   │   ├── version_service.py           # GitHub release polling + admin-triggered update
│   │   └── watchlist_service.py         # CRUD + live quote enrichment
│   ├── tasks/            # APScheduler jobs (US 10s, TW 60s, off-hours throttle).
│   │                     # Discussion-adjacent: ingest_news_tw (hourly,
│   │                     #   Google News RSS zh-TW since PR #128),
│   │                     #   score_news_sentiment (every 30 min, fail-closed cap)
│   ├── tests/            # pytest — in-memory SQLite + AsyncMock Redis
│   │                     # 74 files, 1082 tests. Categories:
│   │                     #   API HTTP   : test_*_api.py        (admin, auth, alerts,
│   │                     #                                      analytics, discussion,
│   │                     #                                      portfolio, tw_market 42,
│   │                     #                                      us_market 23, watchlist)
│   │                     #   Services   : test_*_service*.py   (alert, analytics,
│   │                     #                                      crypto_market, discussion,
│   │                     #                                      news_sentiment, portfolio,
│   │                     #                                      runtime_config,
│   │                     #                                      system_task_config,
│   │                     #                                      tw_market_caching, us_market,
│   │                     #                                      watchlist, llm_key, llm_usage,
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
- US quote: Polygon.io → yfinance → Stooq → Finnhub. Finnhub is the
  4th-tier fallback when both Yahoo and Stooq's Polish edge are
  simultaneously blocked (free tier 60/min, US-east infra). Empty
  `FINNHUB_API_KEY` skips the tier silently — the previous 3-tier
  behaviour is preserved.
- US history / fundamentals / news: Polygon → yfinance → Stooq. Finnhub
  isn't wired into these because its free plan only exposes /quote.
  Options chain is `Polygon → yfinance` only (Stooq has no options endpoint);
  free-tier deployments without a Polygon key still serve chains via
  `yfinance.get_options()` which exposes last_price / IV / OI per contract.
- US screener fallback chain: Polygon snapshot → `_screener_yfinance` (per-
  symbol `.info`, slow + frequently rate-limited) → curated `_FALLBACK_UNIVERSE`
  enriched via `yfinance.get_batch_quotes()` (`yf.download()` chart endpoint,
  more resilient than `.info`) → `stooq.get_batch_quotes()` (free CSV API,
  Polish edge, immune to Yahoo's cloud-IP block) → `finnhub.get_batch_quotes()`
  (per-symbol REST, capped at 4 concurrent in flight). Rows where every
  price is 0 are NOT cached so the next request retries instead of
  locking in 10 min of zeros.
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
- Heavy VaR Monte Carlo / backtest paths run in a lazily-created
  `ProcessPoolExecutor(max_workers=2)` with 30s timeout

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

### Expert discussion subsystem
- **Round-table debate** between N selected personas (2-8, capped). Each
  round walks the roster in order; each persona reads the prior turns
  and replies with structured `{stance: agree|dissent|supplement, content}`
  JSON. Synthesizer at the end produces structured conclusion JSON
  (recommended_symbols / reasoning / risks / time_horizon /
  consensus_score).
- Tables: `discussions` (topic, rules, persona_ids, status, conclusion),
  `discussion_turns` (round, turn_index, persona_id, stance, content,
  citations). Migration `0015_discussions.py`.
- Service: `services/discussion_service.py` — CRUD, `gather_market_context`,
  `run_round` async generator (try/finally guarantees status reset to
  draft even on body exception), `synthesize_conclusion`,
  `extract_focus_symbols` (regex pulls 4-6 digit TW codes from topic
  → injects per-symbol news sentiment alongside market-wide aggregate).
- API: `api/discussion/router.py` — `GET/POST/PATCH/DELETE /sessions`,
  `POST /sessions/{id}/round` (SSE), `POST /sessions/{id}/conclude`.
  Owner-scoped. Quota cost per round = `len(persona_ids)`. Mid-stream
  failure / disconnect refunds `(cost - completed_personas)` so partial
  rounds don't burn the full daily quota.
- Per-persona LLM timeout: `DISCUSSION_PERSONA_TIMEOUT_SECONDS=60`.
  Stuck provider → emit error event, persist placeholder, proceed.
- Persona overrides batch-loaded once per round (`_resolve_persona_specs`)
  so an 8-persona round costs 1 DB query for routing, not 8.
- **Per-round context snapshots** (migration `0023`): `run_round`
  upserts the assembled `gather_market_context` dict into
  `discussion_round_contexts` (PK `(discussion_id, round)`) so re-
  opening an old discussion can show "what data the personas saw at
  the time" instead of re-running the aggregator (which would return
  the *current* market state). Snapshot write failures are logged
  but non-fatal — round still completes. API:
  `GET /api/discussion/sessions/{id}/contexts` returns
  `[{round, context, captured_at}, ...]`, owner-scoped.
- **Per-symbol scoreboard** (migration `0024`,
  `services/discussion_scoreboard_service.py`): the
  `discussions.daily_close_prices` JSON column carries
  `{symbol: [d1, d2, d3, d4, d5]}` closes for each recommended
  symbol so the discussion detail page can render a "對答案" card
  showing day-1 open + 5 daily change %s. Daily cron
  `tasks/score_discussion_outcomes.py` (09:30 UTC) scans concluded
  discussions older than 7 days with NULL `daily_close_prices` and
  fills the column from `ohlcv_daily`. Distinct from the verifier
  (which only grades `auto_run=True` rows for win/loss): this
  covers ALL concluded discussions (manual + auto-run). API:
  `GET /api/discussion/sessions/{id}/scoreboard` returns the
  computed payload — persisted column when present, on-demand
  compute against `ohlcv_daily` when NULL so newly-concluded
  discussions show partial data without waiting a day.
- **Daily auto-run** (`tasks/auto_run_discussion.py`, cron 00:00 UTC):
  per-user opt-in via `discussion_auto_run_configs` (migration `0020`).
  Each user with `enabled=True` gets one `auto_run=True` discussion per
  UTC day, owned by themselves so it surfaces in their own owner-scoped
  sidebar. Topic / rules / persona roster are user-supplied (no
  fallback default). Per-user idempotency: a second tick on the same
  UTC date sees the existing row and skips. One user's failure doesn't
  block others — partial runs report `ok=true row_count=<successes>`
  with the per-user error in the health row's error column.
  Config UI: `AutoRunConfigCard` at the top of `DiscussionPage`'s
  sidebar (collapsed by default).
  API: `GET/PUT /api/discussion/auto-run/config`.

### TW news ingest
- Hourly APScheduler job `tasks/ingest_news_tw.py` pulls TW market
  news from Google News RSS (`hl=zh-TW&gl=TW&ceid=TW:zh-Hant`) — free,
  no token, aggregates cnyes / 經濟日報 / 工商時報 / Yahoo TW / 鉅亨網.
  Replaced FinMind's `TaiwanStockNews` (paid-only) which silently
  rejected free-tier tokens. The FinMind connector is kept for
  institutional / margin / revenue datasets that don't have the same
  paywall. Per-article symbol tagging: regex first 4-6 digit code
  out of the title — same pattern `extract_focus_symbols` uses, so a
  discussion mentioning 2330 picks up titled coverage like
  "台積電(2330)財報".
- Backoff path now preserves the most recent real error in the
  health row (`skipped (...; last: HTTP 429 ...)`) so admins can
  diagnose without clearing Redis to wait out the cooldown.
- Dashboard surfaces ingest output via `GET /api/tw/news/recent`
  (DB-only, market-wide rows with `symbol IS NULL` only) — the
  `RecentTWNews` card on `DashboardPage` renders titles with
  bullish/bearish/neutral sentiment badges from the same row's
  `sentiment_label` column.

### International news ingest
- Sibling cron `tasks/ingest_news_international.py` runs hourly under
  the same Google News RSS zh-TW pipeline but with a Fed / FOMC / 美股 /
  global macro query, writing rows under `market='GLOBAL'`. Symbol
  tags are explicitly stripped (`symbol=NULL` for every row) — the TW
  connector's 4-digit regex would mis-tag years like "2026" as TW
  stock codes and poison `read_symbol_sentiment` lookups. API:
  `GET /api/global/news/recent`.
- `discussion_service.gather_market_context` injects an
  `international_sentiment` block alongside `news_sentiment` (read via
  `read_recent_market_sentiment(market="GLOBAL")`), regardless of the
  discussion's primary market — Fed policy is relevant to TW personas
  just as much as US ones. The DashboardPage replaces the previous
  US/SPY `RecentNews` read-through with a `RecentGlobalNews` card
  backed by this archive so the user sees the same data the personas
  do.

### TAIEX history ingest
- Daily TWSE one-shot cron `tasks/ingest_taiex_history.py` (07:10
  UTC, post-close) pulls the current month's `FMTQIK` series and
  upserts into `ohlcv_daily` under symbol `_TAIEX` (underscore
  prefix marks synthetic / index rows). Reuses the existing
  ohlcv_daily schema + read tier — no new table needed.
- `tw_market_service.get_index(history_days=N)` returns the cached
  current quote plus an N-day daily-close series from the archive.
  `gather_market_context` calls it with `history_days=30` so TW
  personas can reference 大盤型態 ("TAIEX 連跌 5 日 -5%") without
  burning a tool call.

### TW chip metrics ingest
- Two daily TWSE one-shot crons (06:50 + 07:00 UTC, post-close):
  `tasks/ingest_institutional_tw.py` writes 法人買賣超 (foreign /
  SITC / dealer buy + sell volumes) into `tw_institutional_daily`,
  and `tasks/ingest_margin_tw.py` writes 融資融券 (margin purchase +
  balance, short sale + balance) into `tw_margin_daily`. Both use
  TWSE's "all stocks for one day" endpoints — no per-symbol fan-out,
  ~one HTTP call per cron per day.
- Read tier: `tw_market_service.get_institutional` /
  `get_margin` consult Postgres before falling through to FinMind
  per-symbol → TWSE today-only. Saves FinMind quota on the typical
  30-day query path used by the StockDetailPage.
- Discussion context aggregators in `services/ingest/repository.py`:
  `read_top_foreign_buyers` (top 10 net foreign buy over last 5
  trading days) and `read_market_margin_balance_trend` (latest
  market-wide margin + short balance). Wired into
  `gather_market_context` for `market='TW'` only — TWSE-specific
  data shouldn't bleed into a US discussion's prompt context.

### TW industry / company-name enrichment
- The daily `tw_symbol_map` cron now populates two extra in-memory
  maps from TWSE `t187ap03_L` (上市公司基本資料): `_industry_map`
  (symbol → 產業別) and `_name_map` (symbol → 公司簡稱). Memory
  resident — industry rarely changes, no DB needed.
- Accessors: `tw_market_service.get_industry(symbol)` /
  `get_company_name(symbol)`. API: `GET /api/tw/industry/{symbol}`.
- `discussion_service._tag_industry` enriches `top_foreign_buyers`
  / `top_revenue_growers` rows with `industry` + `name_zh` so
  personas can do sector-flow analysis ("外資集中買超半導體業 X 億")
  without an extra LLM tool call. `_compact_screener_row` (used
  for `top_gainers` / `top_losers`) gets the same treatment.

### TW monthly revenue ingest
- Daily FinMind market-wide cron `tasks/ingest_revenue_tw.py` at
  09:00 UTC pulls every listed company's monthly revenue (90-day
  lookback so late filers + corrections land within a week of
  publication) and upserts into `tw_revenue_monthly`. One FinMind
  call (data_id="") returns the entire market — no per-symbol fan-
  out, well below the free-tier hourly limit.
- Read tier: `tw_market_service.get_revenue` consults Postgres
  before falling through to live FinMind per-symbol → MOPS scrape.
- Discussion context aggregator `read_top_revenue_growers` returns
  the top-10 YoY revenue growers in the latest reported month.
  Wired into `gather_market_context` for `market='TW'` only.

### News sentiment scoring
- Hourly APScheduler job `tasks/score_news_sentiment.py` picks up
  `news_articles` rows with NULL `sentiment_score`, batches 20 per LLM
  call, writes back `score ∈ [-1, +1]` + bucket label
  (bullish ≥ 0.25, bearish ≤ -0.25, neutral otherwise) +
  `sentiment_scored_at`.
- Daily LLM-call hard cap via Redis counter
  `sentiment:llm_calls:{YYYYMMDD}` (`SENTIMENT_DAILY_LLM_CALL_CAP=100`,
  TTL 86400). **Fails closed on Redis outage** — skips the pass rather
  than risking unbounded spend.
- Discussion orchestrator reads aggregated sentiment via
  `read_recent_market_sentiment` (market-wide, NULL symbol only) and
  `read_symbol_sentiment` (per stock, used when topic mentions
  specific codes). `gather_market_context` records connector errors
  into `ctx["errors"]` so personas can mention "data was incomplete".

### Runtime tunables (env-var overrides)
- A small subset of `.env` settings can be retuned at runtime via the
  AdminPage `RuntimeTunablesCard`. Stored in `runtime_settings` (migration
  `0017`); resolved through `services/runtime_config_service.py` with a
  60 s Redis cache (cross-pod consistency without explicit invalidation,
  except `upsert` / `delete_override` flush their key to make admin edits
  immediate).
- Currently exposed: `SENTIMENT_DAILY_LLM_CALL_CAP`,
  `DISCUSSION_PERSONA_TIMEOUT_SECONDS`, `AI_REQUESTS_VIEWER_DAILY`,
  `AI_REQUESTS_ANALYST_DAILY`, `FINMIND_HOURLY_REQUEST_LIMIT`. Each has a
  type + min/max bounds enforced at upsert time.
- Adding a new tunable: register a `RuntimeSettingSpec` in `_REGISTRY`,
  switch the call site `settings.X` → `await runtime_config.get_int("X")`.
  The admin UI surfaces it automatically.
- Hot-reload contract: edits propagate within 60 s across all pods via
  Redis cache TTL; a cache `delete` on upsert makes them immediate on
  the originating pod. Settings whose value is captured at module load
  (e.g. `UPDATE_CHECK_INTERVAL_HOURS`, used in `IntervalTrigger`) are
  intentionally NOT in the registry — would mislead admins about
  effect.

### System task LLM routing
- Admins pick the provider/model for each background system task
  (currently `news_sentiment` + `discussion_synthesizer`) via the
  AdminPage `SystemTasksCard`. Same shape as `persona_overrides` but
  kept in a separate `system_task_configs` table (migration `0016`)
  so the persona UI doesn't surface internal tasks as chattable agents.
- Service: `services/system_task_config_service.py` — registry
  (`_TASKS`), `list_tasks`, `upsert_override`, `delete_override`,
  `resolve(task_id) → (provider, model)`, `test_task(task_id)`
  (1-token "ping" for the AdminPage "Test" button).
- Frontend `SystemTasksCard` query auto-refreshes every 30s so two
  admins editing the same task don't drift on different versions.
- Sentiment scorer + synthesizer call `resolve(...)` instead of
  hard-coding the provider/model. Falls back to compiled-in default
  when no override exists. Both record usage events under
  `persona_id="_system:news_sentiment"` /
  `_system:discussion_synthesizer` so background-task cost shows up
  in `UsageCard`.

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

**Note:** the full backend suite is expected to pass once
`requirements.txt` + `requirements-dev.txt` are installed. Some Windows
sandboxed shells block subprocess / multiprocessing primitives used by
`python_exec` and analytics tests; run those tests outside that sandbox if you
see `WinError 5` from process creation.

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
FINNHUB_API_KEY=          # optional — 4th-tier US quote fallback (60/min free)
FINMIND_TOKEN=            # optional — TW institutional data
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
OLLAMA_HOST=http://localhost:11434
GROQ_API_KEY=                    # optional — fast OpenAI-compat (llama-3.3-70b etc)
GROQ_MODEL=llama-3.3-70b-versatile
DEEPSEEK_API_KEY=                # optional — deepseek-chat / deepseek-reasoner
MINIMAX_API_KEY=                 # optional
OPENROUTER_API_KEY=              # optional — multi-provider gateway
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=<password>
DEBUG=false
CORS_ORIGINS=http://localhost:5173
GITHUB_OWNER=x812033727
GITHUB_REPO=FinceptWeb
UPDATE_CHECK_INTERVAL_HOURS=6
UPDATE_COMMAND=                 # empty = /api/admin/update returns "not_configured"
SENTIMENT_DAILY_LLM_CALL_CAP=100         # cap on background sentiment scorer LLM calls per UTC day
DISCUSSION_PERSONA_TIMEOUT_SECONDS=60    # per-persona timeout in a discussion round
```

Per-persona / per-system-task LLM provider+model can also be overridden
at runtime via the AdminPage (`PersonasCard` and `SystemTasksCard`) —
no env-var change required.

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

## Post-deploy smoke test

```bash
SMOKE_EMAIL=admin@example.com SMOKE_PASSWORD=... \
BACKEND_URL=https://fincept.example.com \
python scripts/smoke_test_discussion.py
```

End-to-end walks login → create discussion → SSE round → verify status
reset → conclude → cleanup. Uses real LLM calls so the deployment must
have at least one provider key configured. Exits non-zero on any
assertion failure — drop into a Kubernetes post-deploy hook or CI smoke
job. See script docstring for `--personas`, `--keep`, `--timeout` flags.

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
