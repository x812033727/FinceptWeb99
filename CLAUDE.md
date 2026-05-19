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
│   │   ├── ai_agents/    # SSE streaming chat (23 personas, 8 LLM providers)
│   │   ├── watchlist/    # Multi-watchlist CRUD with live quote enrichment
│   │   ├── alerts/       # Price alert CRUD + check-and-fire
│   │   ├── discussion/   # Multi-persona round-table discussions (SSE rounds + synthesizer)
│   │   ├── system/       # /version (GitHub latest) + /web-vital (Core Web Vitals → Prometheus)
│   │   └── websocket/    # Auth-first WS, Redis pub/sub, delta suppression
│   ├── ai/               # LLM router + agent persona definitions
│   ├── analytics/        # Pure computation: dcf.py, risk.py, backtest.py
│   ├── auth/             # JWT handler + role permissions
│   ├── cache/            # Redis helpers (get/set/delete, key helpers).
│   │                     #   `cache_ttls.py` is the single source of truth
│   │                     #   for all TTL constants (TTL_QUOTE_*, TTL_HISTORY_*,
│   │                     #   TTL_FUNDAMENTALS, TTL_NEWS, TTL_FX_*, …)
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
│   │   │                 #   0024 discussions.daily_close_prices ·
│   │   │                 #   0025-0028 TW chip-flow archive bundle ·
│   │   │                 #   0029-0031 discussion.market consolidation ·
│   │   │                 #   0032 backfill_verify_after_date ·
│   │   │                 #   0033 discussions.as_of_date (backtest mode) ·
│   │   │                 #   0034 signal_audit_history (sparkline source) ·
│   │   │                 #   0035 discussions.post_mortem_conclusion ·
│   │   │                 #   0036 backtest_sweeps · 0037 sweep.auto_post_mortem ·
│   │   │                 #   0038 tw_stock_futures_oi · 0039 tw_vix_daily ·
│   │   │                 #   0040 discussion_lessons · 0041 strategy_templates ·
│   │   │                 #   0042 discussion_sweep_id · 0043 strategy_auto_schedule ·
│   │   │                 #   0044 lesson_regime_tier (PR-B0) ·
│   │   │                 #   0045 sweep_fold_kind (PR-A0) ·
│   │   │                 #   0046 discussion_calibration (PR-C1) ·
│   │   │                 #   0047 sweep_weights_override (PR-A1) ·
│   │   │                 #   0048 strategy_calibration (PR-C2) ·
│   │   │                 #   0049 calibrated_brier (PR-C2 follow-up)
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
│   │                     #   DiscussionTurn, DiscussionAutoRunConfig,
│   │                     #   DiscussionRoundContext, SystemTaskConfig,
│   │                     #   RuntimeSetting, TwInstitutionalDaily,
│   │                     #   TwMarginDaily, TwRevenueMonthly,
│   │                     #   SignalAuditHistory, BacktestSweep,
│   │                     #   TwStockFuturesOi, TwVixDaily
│   ├── services/         # Business logic (cached, waterfall)
│   │   ├── _quote_helpers.py            # Shared market-quote sanitizer (±30%
│   │   │                                #   daily-move bound, used by TW + US)
│   │   ├── alert_service.py             # Price alert CRUD + check_and_fire
│   │   ├── analytics_service.py         # DCF/VaR/backtest orchestration (ProcessPool)
│   │   ├── crypto_market_service.py     # Kraken quote/history/screener (24/7)
│   │   ├── discussion_auto_run_config_service.py # Per-user opt-in for daily auto-run
│   │   ├── discussion_scoreboard_service.py # D1-D5 close array per recommended symbol
│   │   ├── discussion_service.py        # Round-table orchestrator: gather context,
│   │   │                                #   run_round (SSE), synthesize_conclusion,
│   │   │                                #   batch persona resolution, try/finally state
│   │   ├── llm_key_service.py           # DB-first LLM provider key mgmt (Fernet at rest)
│   │   ├── llm_parsing_utils.py         # Shared LLM-output JSON parser (// 註解,
│   │   │                                #   trailing comma, full-width brace, <think>);
│   │   │                                #   used by synthesizer + sentiment scorer
│   │   ├── llm_usage_service.py         # Token + cost tracking (RATE_TABLE)
│   │   ├── market_key_service.py        # DB-first market provider key (FRED, FinMind,
│   │   │                                #   Polygon, Finnhub) + admin Test button
│   │   ├── news_sentiment_service.py    # Hourly batch scoring + market/per-symbol
│   │   │                                #   sentiment aggregators (used by discussion ctx)
│   │   ├── notification_service.py      # Decoupled push dispatcher (WS-registered)
│   │   ├── persona_override_service.py  # Per-persona LLM provider/model overrides
│   │   ├── portfolio_service.py         # CRUD, P&L, multi-currency FX cache, optimiser
│   │   ├── runtime_config_service.py    # Hot-reloadable settings (RuntimeTunablesCard)
│   │   ├── system_task_config_service.py # Admin LLM routing for background tasks
│   │   │                                #   (news_sentiment, discussion_synthesizer)
│   │   ├── signal_audit_service.py      # Citation/value/hallucination audit + history
│   │   │                                #   (PRs #258 #259 #263)
│   │   ├── post_mortem_service.py       # Backtest self-critique: D1-D5 self-eval +
│   │   │                                #   per-day top gainers (PR #273)
│   │   ├── post_mortem_analysis_service.py # Cumulative "缺什麼資料" taxonomy
│   │   │                                #   roll-up across post-mortem turns (PR #262)
│   │   ├── backtest_sweep_service.py    # Multi-day sweep runner: CRUD + worker
│   │   │                                #   + cancel + concurrency (PRs #274 #275)
│   │   │                                #   + fold_kind / weights_override gating
│   │   │                                #   (PRs #341 #345 — A0/A1/A2)
│   │   ├── walk_forward_service.py      # Rolling train/test fold orchestrator
│   │   │                                #   (PRs #341 #345 — A1/A2). plan +
│   │   │                                #   execute_in_background + frozen weights.
│   │   │                                #   Phase 1.5 synchronous train-fold verdict
│   │   │                                #   resolution (audit follow-up) so
│   │   │                                #   weight learner sees real hit rates.
│   │   ├── confidence_calibrator.py     # In-house PAV isotonic regression
│   │   │                                #   (PRs #341 #345 — C2). Per-strategy
│   │   │                                #   curve fitted from rolling
│   │   │                                #   (raw_confidence, outcome) pool.
│   │   │                                #   MIN_SAMPLES_FOR_FIT=30. Applied at
│   │   │                                #   synthesis to write calibrated_confidence.
│   │   ├── lesson_tier_service.py       # Episodic→semantic auto-promotion +
│   │   │                                #   admin-only structural promote
│   │   │                                #   (PRs #341 #343 — B2). Owner-scoped.
│   │   ├── overseas_market_service.py   # SOX/NDX/SPX/DJI/VIX snapshot via yfinance
│   │   │                                #   (PRs #269-#271)
│   │   ├── broker_concentration_service.py # 主力分點 5d aggregate per
│   │   │                                #   focus_symbol (PR #285, FinMind sponsor
│   │   │                                #   live-read + 24h Redis cache, no DB)
│   │   ├── event_calendar_service.py    # Per-symbol + market-wide upcoming
│   │   │                                #   法說/除息 calendar (PRs #238 #284)
│   │   ├── tw_market_service.py         # TW: TWSE → FinMind → MOPS; don't-cache-empty
│   │   ├── tw_trading_calendar.py       # TW market-hours / business-day helpers
│   │   ├── us_market_service.py         # US: Polygon → yfinance → Stooq → Finnhub waterfall
│   │   ├── version_service.py           # GitHub release polling + admin-triggered update
│   │   └── watchlist_service.py         # CRUD + live quote enrichment
│   ├── tasks/            # APScheduler jobs (US 10s, TW 60s, off-hours throttle).
│   │                     # Discussion-adjacent: ingest_news_tw (hourly,
│   │                     #   Google News RSS zh-TW since PR #128),
│   │                     #   score_news_sentiment (every 30 min, fail-closed cap)
│   ├── tests/            # pytest — in-memory SQLite + AsyncMock Redis.
│   │                     # Two autouse fixtures (PR #162) keep tests
│   │                     # isolated: `_override_async_session_local`
│   │                     # monkeypatches every module's `AsyncSessionLocal`
│   │                     # at start, and `_truncate_db_between_tests`
│   │                     # `DELETE FROM` every table at end so cross-test
│   │                     # pollution from `*_autosession` writes is
│   │                     # impossible.
│   │                     # 90 files, 1242 tests. Categories:
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
│       │   ├── admin/    # 8 admin cards extracted from AdminPage (PR #172):
│       │   │             #   SystemUpdate, LLMKeys, MarketKeys, Personas,
│       │   │             #   SystemTasks, RuntimeTunables, Usage, IngestHealth.
│       │   │             #   `_providerModels.ts` shares the LLM catalog.
│       │   ├── charts/   # CandlestickChart (lightweight-charts v4, theme-aware)
│       │   ├── discussion/ # 4 cards from DiscussionPage (PR #175):
│       │   │             #   AutoRunConfigCard, ConclusionCard,
│       │   │             #   RoundContextsCard, ScoreboardCard. `_helpers.tsx`
│       │   │             #   has formatters + summarizeContext + API fetchers.
│       │   ├── layout/   # AppLayout, Sidebar (+ .test), UpdateBadge, NotificationBell
│       │   ├── portfolio/ # AllocationPie, HoldingsTable (+ .test) plus 7 sub-
│       │   │             #   components from PortfolioPage (PR #174):
│       │   │             #   AddTransactionForm, EditTransactionModal,
│       │   │             #   Create/Edit PortfolioModal, ExpertEvaluationCard,
│       │   │             #   PerformanceChart, TransactionHistory.
│       │   ├── stock/    # 10 panels from StockDetailPage (PR #173):
│       │   │             #   Financials, Options (+ inline IVSurface),
│       │   │             #   Institutional, Margin, Revenue, Health,
│       │   │             #   ValuationBand, Holdings, Dividends, NewsFeed.
│       │   │             #   `_shared.ts` carries types + fetchers + formatters.
│       │   └── (root)    # Collapsible (+ .test, PR #168), ErrorBoundary,
│       │                 #   Skeleton, Toaster, DataSourceBadge (each + .test)
│       ├── hooks/
│       │   ├── useWebSocket.ts       # Singleton WS + useAlertSocket() hook
│       │   ├── useWebSocket.test.ts  # 11 tests: connect, routing, alert, cleanup
│       │   ├── usePortfolio.ts       # Portfolio CRUD + optimise mutations
│       │   ├── usePortfolio.test.ts  # 8 tests: query keys, enabled-gate, invalidation
│       │   └── useVersion.ts         # /api/system/version polling for UpdateBadge
│       ├── pages/        # One file per route (13 pages). Most are now thin
│       │                 # composition shells after the Tier-3 page split
│       │                 # (PRs #172-#175); business components live under
│       │                 # `components/{admin,stock,portfolio,discussion}/`.
│       │   # AIPage, AdminPage (233 LOC), AlertsPage, AnalyticsPage, DashboardPage,
│       │   # LoginPage, MacroPage, MarketPage, PortfolioPage (310 LOC), ScreenerPage,
│       │   # SettingsPage, StockDetailPage (359 LOC), WatchlistPage
│       │   # DiscussionPage (787 LOC — keeps the SSE state machine + transcript)
│       ├── store/        # Zustand. Each *Store.ts pairs with *Store.test.ts:
│       │                 #   authStore, notificationStore, themeStore, toastStore
│       │                 # Plain modules (no test): analytics, market, portfolio, system
│       ├── test/         # Vitest setup (jsdom + RTL cleanup)
│       ├── types/        # TypeScript interfaces — market, portfolio, analytics,
│       │                 # system, **discussion** (PR #171: shared types for
│       │                 # AgentInfo, Discussion, Conclusion, ScoreboardRow, etc.)
│       └── lib/          # api.ts (+ .test: bearer/refresh/dedup), auth.ts (silentRefresh),
│                         # formatters.ts (+ .test, PR #166: shared formatPct /
│                         #   formatNumber / formatCompact — defaults match
│                         #   backend percent-units convention),
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
2. Redis (quotes 15s, history 4h, fundamentals 24h, screener 10m, news 5m).
   All TTL constants live in `backend/cache/cache_ttls.py` — tune there
   instead of editing call sites.
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
- **23 personas** across two groups:
  - 7 CFA-style functional: market_analyst, portfolio_advisor, risk_manager,
    macro_analyst, earnings_analyst, trading_coach, claude_research
  - 16 legendary investors / traders: buffett, graham, munger (value); lynch,
    fisher, smith (quality-growth); marks, klarman (contrarian); dalio, soros
    (macro); simons, asness (quant); livermore, ptj, minervini, raschke
    (short-term trading — tape reading, breakout momentum, swing patterns,
    hard stops). The short-term group fills the day-to-week gap that the
    long-horizon roster can't speak to; they share `_SHORT_TERM_PROFILE`
    (= `_QUANT_PROFILE | per_symbol_news_sentiment`) so personas see news
    context for tape reading without dragging in the value-investing blocks.
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
- **Daily auto-run** (`tasks/auto_run_discussion.py`, cron 20:00 UTC =
  04:00 Asia/Taipei next day — 5h before TW market open):
  per-user opt-in via `discussion_auto_run_configs` (migration `0020`).
  Each user with `enabled=True` gets one `auto_run=True` discussion per
  Taipei calendar day, owned by themselves so it surfaces in their own
  owner-scoped sidebar. Topic / rules / persona roster are user-
  supplied (no fallback default). Per-user idempotency keyed on the
  Taipei calendar day (half-open UTC range via
  `tw_trading_calendar.tw_day_utc_bounds`): a second tick on the same
  Taipei date sees the existing row and skips. One user's failure
  doesn't block others — partial runs report `ok=true
  row_count=<successes>` with the per-user error in the health row's
  error column.
  Config UI: `AutoRunConfigCard` at the top of `DiscussionPage`'s
  sidebar (collapsed by default).
  API: `GET/PUT /api/discussion/auto-run/config`.
- **Post-mortem self-critique** (PR #249 + #266 + #267 + #272 + #273):
  for backtest discussions only. After /conclude, the user can hit
  「事後檢討」 to chain (a) build_post_mortem_message →
  (b) inject user_input prompt → (c) run new round → (d) re-synthesize.
  PR #273 evolved the prompt from a single-day "next-day top 5" to
  the **5-trading-day window** with two ground-truth surfaces:
  *recommended self-eval* (per pick D1-D5 close changes vs as_of)
  and *daily top-N gainers* (each day's #1-5 vs prior-day close).
  PR #272 routes the re-synthesize output to a separate
  `discussions.post_mortem_conclusion` JSONB column (migration `0035`)
  so the original `conclusion` is preserved for side-by-side
  comparison instead of being overwritten. UI surfaces both in
  ConclusionCard via a `variant: "primary" | "post_mortem"` prop —
  primary stays amber, post_mortem is purple matching the
  RoundSection 「📋 事後檢討」 badge introduced in PR #268.
  API: `POST /api/discussion/sessions/{id}/post-mortem`.
- **Multi-day backtest sweep** (`backtest_sweeps` table, migrations
  `0036` + `0037`, PRs #274 + #275): operator picks an anchor date
  + N trading days + M rounds-per-discussion + concurrency 1-3 +
  `auto_post_mortem` flag (default ON). Worker is a detached
  `asyncio.create_task` fired by /sweeps/{id}/start; resolves the
  actual N trading days from `ohlcv_daily`, then for each date
  spawns a new Discussion with `as_of_date=that_date`, runs M
  rounds, synthesises, and (when `auto_post_mortem`) chains the
  post-mortem self-critique on top. Cancel mid-flight via
  /sweeps/{id}/cancel — worker checks status between chunks
  and bails leaving partial progress in `completed_dates` /
  `failed_dates`. Failures are isolated per date (one bad date
  doesn't halt the sweep). Concurrency capped at 3 — going higher
  saturates LLM provider rate limits and burns daily quota
  proportionally; default 1 (serial) is the recommended setting.
  Config UI: `BacktestSweepCard` under `AutoRunConfigCard` in the
  DiscussionPage left rail.
  API: `GET/POST /api/discussion/sweeps`,
  `POST /api/discussion/sweeps/{id}/start`,
  `POST /api/discussion/sweeps/{id}/cancel`,
  `DELETE /api/discussion/sweeps/{id}`.

### Walk-forward OOS validation (PRs #341 #345 — A0 + A1 + A2)
- **Why**: The legacy weight learner (PR-C, persona_weight_learner)
  retrained on the same sweep it was evaluating — classic
  in-sample overfitting. Walk-forward splits each strategy's
  history into rolling train/test windows so weights learned on
  the train slice are evaluated on a disjoint future slice, then
  promoted to production only when the OOS check survives.
- **Schema** (migrations `0044`-`0047`):
  - `backtest_sweeps.fold_kind ∈ {train, test, production}`
    (default `production`; existing sweeps untouched)
  - `backtest_sweeps.parent_sweep_id` (test → train link)
  - `backtest_sweeps.weights_override JSONB` — frozen persona
    weights for the test fold, NEVER written back to the
    template's `persona_weights` (that's the OOS-cleanness
    invariant)
- **Service**: `services/walk_forward_service.py`
  - `plan_walk_forward(strategy, anchor, train_window=60d,
    test_window=20d, n_folds=2)` — pure read; rejects with
    `ValueError` when `ohlcv_daily` can't reach back
    `(train+test) × n_folds × 2` calendar days from the anchor
  - `execute_walk_forward_in_background(...)` — fire-and-forget
    asyncio task. Per fold: spawn `train` sweep → await worker
    → SYNCHRONOUSLY verify train fold's discussions (audit
    follow-up: cron is too slow, would leave verdicts NULL and
    weight learning would collapse to uniform) → fit weights
    via `compute_weights_from_aggregate` → spawn `test` sweep
    with `weights_override` + `parent_sweep_id` → await worker
- **Phase 3 gate**: production sweeps with `weights_override`
  set (auto-schedule via `latest_validated_weights(...)` from
  PR-A2) skip the in-sample retrain so OOS-clean weights don't
  get re-poisoned by their own results
- **Concurrency guard**: `has_active_walk_forward(strategy_id)`
  blocks parallel runs on the same strategy via 409
- **Observability**: `WALK_FORWARD_RUNS_TOTAL{status}` and
  `WALK_FORWARD_FOLDS_TOTAL{outcome}` Prometheus counters,
  structured `walk_forward.start` / `walk_forward.complete`
  logs bookending each run
- API: `POST /api/discussion/strategies/{id}/walk-forward`
  (validates inputs, returns the resolved fold plan + 200
  immediately; orchestrator runs detached). UI: `WalkForward
  Section` inside StrategyTemplateCard, side-by-side
  in-sample-vs-OOS compare in SweepAggregateCard.

### Confidence calibration (PRs #341 #345 — C0 + C1 + C2)
- **Why**: LLM-emitted confidence is well-known to be poorly
  calibrated (over-confident at the high end). Brier score on
  raw confidence isn't actionable until we measure WHETHER
  calibration is helping; without that side-by-side, every
  improvement claim is anecdotal.
- **C0 — emission**: `synthesize_conclusion`'s prompt asks for
  `recommendations: [{symbol, confidence}]` per pick. Parser
  back-fills 0.5 (neutral) when an old discussion lacks the
  field — the structured shape is guaranteed downstream.
- **C1 — Brier**: `discussion_scoreboard_service.compute_brier_
  for_discussion` produces `discussions.brier_score` over raw
  confidence and `discussions.outcome_vector` (per-symbol
  `{symbol, confidence, outcome_binary, peak_pct,
  calibrated_confidence?}`). NULL when partial coverage. Sweep
  aggregate rolls up sample-weighted means + 10-bucket
  reliability diagram.
- **C2 — isotonic curve** (per strategy):
  `services/confidence_calibrator.py` fits in-house PAV (Pool
  Adjacent Violators, ~30 lines, no sklearn) over the rolling
  pool of (raw_confidence, outcome_binary) pairs from every
  resolved child discussion across the strategy's sweeps.
  `MIN_SAMPLES_FOR_FIT = 30`; below threshold the curve stays
  NULL and synthesis emits raw only. Triggered from sweep
  worker Phase 3 alongside the weight learner. Curve persisted
  on `discussion_strategy_templates.calibration_curve` (JSONB
  list of `[{raw, calibrated}]` control points).
- **C2 wire-through**: `synthesize_conclusion` applies the
  curve to every recommendation's raw confidence and stores
  `calibrated_confidence` alongside; raw is preserved for
  future re-fits (key invariant — never feed calibrated values
  back into the next fit, that would self-degenerate to
  identity).
- **Calibrated brier** (migration `0049`): `discussions.
  calibrated_brier_score` parallel to raw brier. NULL when any
  pick lacks `calibrated_confidence` (partial coverage); a
  meaningful comparison requires complete coverage. Aggregate
  rolls up alongside raw. **The diagnostic signal**: when
  `calibrated_brier < raw_brier`, the curve is reducing error;
  when `calibrated_brier > raw_brier`, the curve is mis-fitting
  (often regime drift) — the SweepAggregateCard's BrierRow
  inline-renders this as ✔/⚠.
- API: emerges through the existing `GET /api/discussion/
  sweeps/{id}/aggregate` and `GET /api/discussion/strategies/
  {id}/aggregate` payloads (added fields `brier_score`,
  `calibrated_brier_score`, `reliability`, `fold_kind`,
  `parent_sweep_id`).

### Sponsor-tier ctx blocks (PRs #282-#285)

Layered onto the discussion ctx after the user's FinMind Sponsor
upgrade. Each closes one item from PR #262's post-mortem-gap
taxonomy. All 4 register their keys into
`signal_audit_service._SIGNAL_KEYWORDS` so PR #263's sparkline
trend cron picks them up automatically — no separate
observability wiring per block.

- **個股期貨 三大法人未平倉** (`single_stock_futures_oi`, PR #282,
  migration `0038`). FinMind `TaiwanFuturesInstitutionalInvestors`
  with `data_id=""` returns ALL contracts in one Sponsor call;
  `tasks/ingest_stock_futures_oi_tw.py` filters out index futures
  (TX/MTX/TE/TF/...) and aggregates per-investor-type rows
  (foreign / SITC / dealer + their sub-types) into one row per
  (stock, date) in `tw_stock_futures_oi`. Read tier
  `read_top_foreign_stock_futures_buyers` returns top 10 by
  5-day foreign-net-OI delta. Per-stock futures lead spot
  moves by 1-2 days the same way taifex_positioning leads
  TAIEX.
- **TAIWAN VIX** (`taiwan_vix`, PR #283, migration `0039`).
  TAIFEX public CSV download — free + public, not in FinMind's
  catalogue. New `data/tw/taifex_connector.py` module hosts the
  scraper (Big5-encoded, tolerant of column-order rotations).
  Cron `tasks/ingest_tw_vix.py` pulls a 30-day window daily into
  `tw_vix_daily`. Read tier returns latest + 5-day change.
  Sits alongside `overseas_indicators` `^VIX` so personas can
  spread TW vs US implied-vol regime ("台 VIX 22 vs ^VIX 16
  → idiosyncratic 風險偏高").
- **法說會 / 除息行事曆 (market-wide)** (`upcoming_events_calendar`,
  PR #284). New `event_calendar_service.get_market_upcoming_events`
  fans out the existing per-symbol yfinance calendar (PR #238)
  across the symbols already in ctx (`top_foreign_buyers` +
  `top_revenue_growers` + `single_stock_futures_oi` +
  `focus_briefs` + `focus_symbols`) — biases coverage toward
  what's actionable for THIS discussion, not a static large-cap
  list. Output is sorted soonest-first, ties broken by symbol,
  capped at 20. Each per-symbol read hits the existing 24h
  cache, so a multi-discussion sweep over the same trending
  stocks only fans yfinance once per symbol per day.
- **主力分點** (`broker_concentration`, PR #285, **no DB table**).
  Live FinMind `TaiwanStockTradingDailyReport` per-symbol with
  Redis 24h cache. Architectural divergence from #282-#284:
  per-stock fan-out at scale = ~1700 calls/day = the entire
  Sponsor hourly budget; we don't need that for a per-stock
  signal that only fires for `focus_symbols`. Per-discussion
  fan-out hard-capped at 5 symbols (`_DEFAULT_MAX_SYMBOLS`).
  Service splits aggregated rows into top_buyers (positive
  net) + top_sellers (negative net), sentinels `_no_data` for
  symbols FinMind has no breakdown on. Backtest mode just
  queries FinMind with the historical date range (the
  date-range query handles the as_of clamp inherently;
  service additionally drops `date > anchor` rows defensively).

### FinMind clone subsystem (`backend/finmind/`, PRs #288 #289)

Self-contained "FinMind-as-a-paid-service" sub-app. Lives in its
own Postgres DB (`FINMIND_DATABASE_URL`, port 5433 via
`postgres_finmind` docker-compose service under profile `finmind`)
with its own Alembic version table (`alembic_version_finmind`).
Zero schema overlap with the main `finceptweb` DB — different Base,
different session factory, different migrations.

**Path A architecture**: stays a separate database forever (not a
schema in the main DB). Future microservice extraction is one
`pg_dump | pg_restore` away. Main backend imports `finmind.api.router`
to mount the public endpoints; internal tools call the subsystem
via HTTP, not in-process imports.

**Path A2: shared main DB with `finmind` schema (default since PR #313)** —
`FINMIND_USE_MAIN_DB=true` is the default. The FinMind engine binds
to the main app's `DATABASE_URL` with all FinMind tables (and the
`alembic_version_finmind` ledger) in a dedicated `finmind` Postgres
schema for namespace isolation. Implemented via
`SET search_path TO finmind, public` on every checked-out connection
so application queries don't need to qualify table names.
Architectural separation preserved — `pg_dump --schema=finmind` ports
the data to a standalone DB whenever the deployment grows enough to
warrant it.

**Path A1: separate `postgres_finmind` container (opt-in)** — set
`FINMIND_USE_MAIN_DB=false` and run `docker compose --profile finmind
up -d postgres_finmind`. Larger deployments that want isolation at
the database level. Default flipped away from this in PR #313
because every deploy that didn't bring up the sidecar container
(majority case) was hitting `gaierror` on the `postgres_finmind`
hostname.

**Migration**: deploys that ran with the old default (Path A1
implicit) had data in `postgres_finmind:5432/finmind_clone`. After
PR #313 they will silently switch to writing into the main DB's
`finmind` schema unless they set `FINMIND_USE_MAIN_DB=false`
explicitly. The lifespan log line surfaces the active mode at
startup so the operator can react. To migrate the old data in
without losing it:
```bash
pg_dump -h <old_finmind_host> -U finmind -d finmind_clone --schema=public \
  | psql -h <main_db_host> -U fincept -d finceptweb \
       --set ON_ERROR_STOP=on -c "SET search_path TO finmind"
```
Or just let auto-init re-seed (the catalog is rebuildable; the
ingest progress ledger gets re-derived from the destination tables
on next cron tick).

**Deployment env-var checklist**: three env vars control the
subsystem and MUST be propagated through the deployment glue (the
class of bug we hit pre-PR #304: setting `FINMIND_USE_MAIN_DB=true`
in `.env` had no effect because `docker-compose.yml` and the Helm
configmap didn't pass it through).

  - `FINMIND_USE_MAIN_DB` — Path A2 (`true`, default since PR #313)
    vs A1 (`false`).
  - `FINMIND_AUTO_INIT` — auto-run alembic + seed on lifespan.
  - `FINMIND_DATABASE_URL` — only consulted when A1; default points
     at `postgres_finmind:5432` inside the compose network.

  Wired in:
  - `docker-compose.yml backend.environment` — explicit
    `${VAR:-default}` passthrough lines.
  - `helm/.../templates/configmap.yaml` — generic loop over
    `.Values.env.backend`, so adding a new var = edit `values.yaml`
    only.
  - `.env.example` — "FinMind Clone Subsystem" stanza.

  Verification: backend lifespan now logs
  `finmind config: USE_MAIN_DB=..., effective_url=..., schema=...,
  AUTO_INIT=...` immediately after auto-init. Single `grep` confirms
  whether env-var propagation actually reached the process.

**Multi-pod deploys MUST set `FINMIND_AUTO_INIT=false`**: with
`FINMIND_AUTO_INIT=true` (the default), every backend pod runs
`alembic upgrade head` on startup. Two pods racing the upgrade
relies on alembic's per-DB version-table lock — if Pod A acquires
the lock and crashes mid-migration (OOM kill, network partition),
Pod B times out and the DB is left half-migrated.

**Severity escalated since PR #313**: with `FINMIND_USE_MAIN_DB=true`
the default, the alembic race now happens against the **main
production DB**, not an isolated `finmind_clone` DB. A failed
migration there can leave your main app's schema half-applied —
catastrophic. Operators on horizontal-scaling deployments must
opt out of auto-init explicitly:

  1. Add a Kubernetes pre-deploy `Job` (or use the existing compose
     `migrate` service shape) that runs `python -m finmind.scripts.init_db`
     to head BEFORE rolling out the backend Deployment.
  2. Set `FINMIND_AUTO_INIT=false` on the backend Deployment so no
     pod re-runs migrations during rolling restarts.
  3. The lifespan verification log line still prints with
     `AUTO_INIT=False`, so the operator can confirm the opt-out is
     effective.

Single-pod deployments (default `docker compose up`) keep
`FINMIND_AUTO_INIT=true` — the race doesn't exist with one pod.

**Module map** (everything under `backend/finmind/`):

| Module | Purpose |
|---|---|
| `config.py` | `FinmindSettings` — separate Pydantic Settings |
| `db/{base,session}.py` + `db/migrations/` | Independent DeclarativeBase, `FinmindAsyncSessionLocal`, isolated Alembic with `version_table='alembic_version_finmind'` |
| `dataset_catalog.py` | 80 FinMind datasets → local table + Phase A/B sources mapping; validates against `data.tw.finmind_datasets` at import |
| `models/` | 9 ORM files: dataset_source, backfill_progress, master, technical, chip, fundamental, derivative, corporate, intraday, billing |
| `ingest/` | Phase A FinMind backfill runner — `runner.ingest_chunk` is the single entry point; `mappings.py` has 19 dataset → local-table column maps (5 row_transform + 4 batch_transform pivots); `progress.py` is the resumable chunk ledger; `selfcrawl/` registers Phase B clients (`twse.py` wires 5 datasets) |
| `scheduler/` | Auto-runner — `dispatcher.is_due` (pure due-detection), `runner.run_due_now` (DB-aware), `runner.get_universe_from_tw_stock_info` (auto-discover symbols) |
| `billing/` | `keys.py` (issue/verify `fck_live_` API keys, sha256 + prefix), `quota.py` (per-key daily call+row counter), `stripe_webhook.py` (signature verify + event dispatch — no `stripe` SDK) |
| `api/` | `router.py` mounts `/api/finmind/*` endpoints; `auth.py` resolves env-allowlist + `fck_live_` keys; `schemas.py` Pydantic responses |
| `scripts/` | `init_db` (migrate + seed catalog), `status` (--watch / --json health report), `check` (cron exit-code), `backfill` (manual ingest), `run_due` (cron auto-runner) |
| `tests/` | 172 tests, in-memory SQLite |

**Migrations** (`finmind/db/migrations/versions/`):

  - 0001 `dataset_sources` (routing table)
  - 0002 `backfill_progress` (resumable ledger)
  - 0003-0009 destination tables — 66 across master / technical /
    chip / fundamental / derivative / corporate+CB+news / intraday
  - 0010 enable TimescaleDB compression on every hypertable
    (segmentby='market, symbol' for daily, retention 7d for
    tick + broker_daily, 30d for daily-grain, 60d for market-wide)
  - 0011 billing schema — `plans`, `subscriptions`, `api_keys`,
    `api_usage_events` (hypertable), `payment_events`

**Public endpoints** (`/api/finmind/...`):

  - `GET /datasets` — catalog discovery (no quota cost)
  - `GET /data/{dataset_code}?data_id=&start_date=&end_date=&limit=`
    — FinMind-mirror response shape `{status, msg, data, metadata}`,
    metadata adds `last_ingest_at` + `active_source` so callers can
    reason about freshness without a separate call. Quota-gated for
    `fck_live_` keys with `X-RateLimit-Limit/-Remaining/-Reset`
    headers + 429 + Retry-After when exhausted.
  - `GET /data/{dataset_code}/export?format=csv|jsonl&...&limit=`
    — bulk streaming up to 1M rows. CSV (RFC 4180 quoted) or NDJSON.
    Memory flat regardless of size (5K-row chunked yields).
    Soft cap clamps to remaining row quota; hard cap 1M protects DB.
  - `GET /admin/datasets` + `PATCH /admin/datasets/{code}` —
    operator gate via `FINMIND_ADMIN_API_KEY`. Toggle `enabled` /
    flip `active_source` (Phase A → B per dataset, no code change).
  - `POST /webhooks/stripe` — verifies `Stripe-Signature` (HMAC-
    SHA256, ±5min tolerance), dedups via UNIQUE (provider, event_id),
    dispatches subscription.{created,updated,deleted} +
    invoice.payment_failed.

**Auth tiers**:

  1. `X-Finmind-API-Key: fck_live_<32>` → resolved against `api_keys`
     (sha256 + prefix lookup, hmac compare). Quota applies.
  2. `X-Finmind-API-Key` matches `FINMIND_API_KEYS_ALLOWLIST` env
     var → operator/dev key, no quota.
  3. `DEBUG=true` + empty allowlist → open access for local dev.

  All three log to `api_usage_events` for analytics; only #1 enforces
  quota + populates `X-RateLimit-*` headers.

**Operator wiring** (production):

```bash
# 1. Bring up the isolated DB (one-time)
docker compose --profile finmind up -d postgres_finmind

# 2. Migrate + seed catalog (idempotent, safe to re-run)
cd backend && python -m finmind.scripts.init_db

# 3. Configure the FinMind sponsor token via .env or admin UI

# 4. Daily auto-run via cron (universe self-discovers from
#    tw_stock_info as long as TaiwanStockInfo is enabled too)
*/15 * * * * cd /app/backend && \
    python -m finmind.scripts.run_due --universe-from-tw-stock-info

# 5. Issue customer keys
python -c "import asyncio; from finmind.db.session import \
    FinmindAsyncSessionLocal; from finmind.billing.keys import issue_key; \
    asyncio.run(...)"
```

**Phase A → B cutover** (when FinMind subscription expires):

  Single `PATCH /admin/datasets/{code}` per dataset:
  ```
  PATCH active_source: 'finmind' → 'twse'
  ```
  No code change — runner picks `selfcrawl.resolve_client(source)`.
  Currently 5 datasets ready: TaiwanStockPrice,
  TaiwanStockInstitutionalInvestorsBuySell,
  TaiwanStockMarginPurchaseShortSale, TaiwanStockInfo, TaiwanStockPER.
  Adding more is one entry per dataset in `selfcrawl/twse.py:_DISPATCH`.

**Status snapshot**:

  - Schema: 66/80 destination tables built (realtime + sponsor-only-
    no-self-crawl + derived skipped by design)
  - Mappings: 19/80 datasets have a Phase A FinMind ingest mapping
    (5 headline + 10 direct + 4 wide-format pivots — the rest are
    sponsor-only / niche, append one entry to add)
  - Phase B: 5 datasets via TWSE self-crawl + 1 via MOPS (revenue);
    TPEX / TAIFEX / TDCC remain `_NotWiredYetClient` stubs.
    `is_source_implemented()` blocks AdminPage flips to stubs (PR
    #306) so an operator can't break the cron silently.
  - Tests: 259 finmind subsystem tests + ~50 main-app integration
    tests touching the FinMind clone (admin proxy, lifespan,
    discussion ctx blocks)
  - Deferred: Stripe Checkout / Customer Portal end-user flow
    (schema ready, webhook handler in place — depends on real Stripe
    account for end-to-end testing); TPEX / TAIFEX / TDCC self-crawl
    handlers (stubs in selfcrawl/__init__, AdminPage refuses to
    flip to them until wired); deep MOPS coverage beyond monthly
    revenue (FinancialStatements / BalanceSheet / CashFlow scrapers).
    AdminPage's FinmindAdminCard / UsageCard / KeysCard are all
    shipped + tested.

**TimescaleDB compression** is OFF by default in the main DB (not
applied to `ohlcv_daily` etc.) but ON in the FinMind clone DB —
migration 0010 enables it on every hypertable. Expected ratios:
daily ~22×, tick ~15×, broker_daily ~20×.

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
FINMIND_USE_MAIN_DB=false # set true to share main DATABASE_URL via `finmind` schema instead of running postgres_finmind separately
FINMIND_AUTO_INIT=true    # set false to opt out of lifespan auto-migrate + seed (multi-pod deploys)
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

## Backfilling historical news (FinMind paid tier)

The hourly Google News RSS ingest only sees the last ~14 days, which
is why backtest discussions anchored at older dates surface the
"news archive doesn't reach this date" warning. With a paid FinMind
sponsor token (`FINMIND_TOKEN` env var or admin-panel-stored DB
key), `scripts/backfill_news_finmind.py` pulls the entire historical
`TaiwanStockNews` archive (FinMind starts ~2017) into `news_articles`
in monthly chunks:

```bash
cd backend
# the last 2 years (typical first backfill)
python -m scripts.backfill_news_finmind --start 2024-01-01 --end 2026-04-30

# a narrow window — e.g. just the discussion's anchor week
python -m scripts.backfill_news_finmind --start 2026-03-25 --end 2026-04-05
```

Idempotent: sha256(title+link) dedup at the insert layer means
re-running the same range writes nothing the second time. Backfilled
rows carry `source="finmind_backfill"` so the admin dashboard can
distinguish them from regular Google News RSS rows
(`source="google_news_rss"` etc.) when investigating coverage.

Sentiment scoring: backfilled rows arrive with `sentiment_score=NULL`.
The hourly `tasks.score_news_sentiment` cron picks them up at the
configured `SENTIMENT_DAILY_LLM_CALL_CAP` rate (default 100/day). For
a fresh ~50k-row backfill, bump the cap via the AdminPage
`RuntimeTunablesCard` for a one-time burn — the runtime config
propagates within 60 s and you can drop it back afterwards.

## Backfilling historical monthly revenue (FinMind paid tier)

The daily `ingest_revenue_tw` cron only pulls a 90-day FinMind
lookback, and FinMind's market-wide `TaiwanStockMonthRevenue` query
was paywalled in 2026-04 — when that happens the cron returns early
without writing anything, so any bogus values sitting in
`tw_revenue_monthly` from a previous deploy persist indefinitely.
With a paid FinMind sponsor token, `scripts/backfill_revenue_finmind.py`
walks the historical archive (~2017+) into `tw_revenue_monthly` in
30-day chunks **oldest → newest**, so each chunk's `_enrich_growth_rates`
pass can compute YoY against rows already-backfilled by previous chunks:

```bash
cd backend
# the last 3 years (typical first run — covers most TW listed companies'
# YoY computability)
python -m scripts.backfill_revenue_finmind --start 2023-01-01 --end 2026-04-30

# or extend further back (FinMind starts ~2017)
python -m scripts.backfill_revenue_finmind --start 2017-01-01 --end 2026-04-30
```

The upsert overwrites in place via `(market, symbol, ts)` PK, so:
  - re-runs are idempotent (no duplicates),
  - bogus rows from earlier deploys (e.g. PR #211 fix-target where
    `revenue_yoy=2026` was the year integer leaked through the
    connector) get overwritten with the correctly-computed value
    (or NULL when the prior-year baseline still isn't in the archive),
  - `source="finmind_backfill"` distinguishes backfilled rows from
    daily-cron rows when investigating coverage.

## Seeding trader strategy templates

`scripts/seed_trading_strategies.py` creates 6 well-known trader
strategy templates under a target user (default: `settings.ADMIN_EMAIL`).
Each maps to existing personas in `ai/agents.py` and goes through
`strategy_template_service.create_template` so bounds are validated at
insert.

```bash
cd backend
# default — seeds under settings.ADMIN_EMAIL
python -m scripts.seed_trading_strategies

# seed under a different existing user
python -m scripts.seed_trading_strategies --email user@example.com
```

Strategies covered: 動量突破 (Minervini + Livermore, TW), 宏觀趨勢追蹤
(PTJ + Dalio, GLOBAL), 均值回歸拉回 (Raschke, TW), 價值投資護城河
(Buffett + Graham + Munger, US), 量化多因子 (Simons + Asness, US),
反向危機投資 (Klarman + Marks + Soros, GLOBAL).

Idempotency: skips presets whose `name` already exists for the owner
(active templates only — soft-deleted ones don't block recreation).
Re-running after a manual delete recreates the missing template; an
untouched re-run is a no-op. Templates remain owner-scoped, so seeding
under one user doesn't surface them in another user's sidebar — to
share a strategy across users, run the script per email.

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
