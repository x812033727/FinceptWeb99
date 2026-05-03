from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://fincept:password@localhost:5432/finceptweb"

    # Redis
    REDIS_URL: str = "redis://localhost:6379"

    # Security
    JWT_SECRET_KEY: str = "change_me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    @field_validator("JWT_SECRET_KEY")
    @classmethod
    def validate_jwt_secret(cls, v: str) -> str:
        import os
        debug_on = os.environ.get("DEBUG", "").lower() in ("true", "1")
        if not v or v == "change_me":
            if debug_on:
                return v
            raise ValueError(
                "JWT_SECRET_KEY must be set to a strong random value "
                "(min 32 chars) in production. Set DEBUG=true only for local dev."
            )
        # Length check now applies in BOTH debug and prod — DEBUG only allows
        # the literal placeholder, never a short real-looking secret.
        if len(v) < 32:
            raise ValueError("JWT_SECRET_KEY must be at least 32 characters")
        return v

    # CORS
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",")]

    # US market data
    POLYGON_API_KEY: str = ""
    YAHOO_FINANCE_ENABLED: bool = True
    FRED_API_KEY: str = ""
    # Finnhub: 4th-tier fallback when Polygon / yfinance / Stooq all fail.
    # Free tier is 60 req/min — plenty for the 25-symbol screener cap. Empty
    # key disables the connector entirely (no degradation banner change).
    FINNHUB_API_KEY: str = ""

    # TW market data
    FINMIND_TOKEN: str = ""
    TWSE_API_ENABLED: bool = True

    # AI / LLM
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    OLLAMA_HOST: str = "http://localhost:11434"
    DEFAULT_LLM_PROVIDER: str = "openai"  # openai | anthropic | gemini | ollama | minimax | groq | deepseek | openrouter

    # MiniMax (OpenAI-compatible chat completions + tool-use). Empty key disables the provider.
    MINIMAX_API_KEY: str = ""
    MINIMAX_HOST: str = "https://api.minimax.io"  # international; mainland: https://api.minimax.chat
    MINIMAX_MODEL: str = "MiniMax-M2.7"  # alts: MiniMax-M2.7-highspeed, abab6.5s-chat, abab6.5-chat, MiniMax-Text-01
    MINIMAX_MAX_TURNS: int = 6  # tool-loop ceiling so a misbehaving model can't burn the quota

    # Groq (OpenAI-compatible; very fast inference). Empty key disables.
    GROQ_API_KEY: str = ""
    GROQ_HOST: str = "https://api.groq.com/openai"
    GROQ_MODEL: str = "llama-3.3-70b-versatile"  # alts: llama-3.1-70b-versatile, mixtral-8x7b-32768
    GROQ_MAX_TURNS: int = 6

    # DeepSeek (OpenAI-compatible; cheap, strong numerical reasoning). Empty key disables.
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_HOST: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-chat"  # alts: deepseek-reasoner (R1)
    DEEPSEEK_MAX_TURNS: int = 6

    # OpenRouter (OpenAI-compatible meta-router; one key, 100+ models). Empty key disables.
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_HOST: str = "https://openrouter.ai/api"
    OPENROUTER_MODEL: str = "openai/gpt-4o-mini"  # always model = "<provider>/<model>"
    OPENROUTER_REFERER: str = ""  # optional HTTP-Referer for OpenRouter analytics
    OPENROUTER_TITLE: str = ""    # optional X-Title for OpenRouter analytics
    OPENROUTER_MAX_TURNS: int = 6

    # Encryption key for at-rest LLM provider keys stored in the DB. Empty
    # means "derive from JWT_SECRET_KEY" — see auth/llm_key_crypto.py.
    LLM_KEY_ENCRYPTION_KEY: str = ""

    # Claude Agent (tool-use via claude-agent-sdk). Auto-disabled when the
    # optional `claude-agent-sdk` package isn't importable so a deployment
    # without the SDK never surfaces 500s — see `claude_agent_effective_enabled`.
    # Analyst/admin role required regardless.
    CLAUDE_AGENT_ENABLED: bool = True
    CLAUDE_AGENT_MODEL: str = "claude-sonnet-4-5-20250929"
    CLAUDE_AGENT_MAX_TURNS: int = 8
    CLAUDE_AGENT_TOOL_TIMEOUT_S: int = 30
    CLAUDE_AGENT_PYTHON_MEM_MB: int = 256
    CLAUDE_AGENT_PYTHON_CPU_S: int = 5
    # Hosts the `web_fetch` tool may reach. Curated default covers GitHub
    # raw content, Anthropic + FastAPI docs, and the FRED / SEC / Yahoo
    # public data hosts the agent commonly needs. Operators tighten or
    # widen via env. Setting to an empty string still blocks all fetches.
    CLAUDE_AGENT_WEBFETCH_ALLOWLIST: str = (
        "raw.githubusercontent.com,api.github.com,docs.anthropic.com,"
        "fastapi.tiangolo.com,api.stlouisfed.org,www.sec.gov,query1.finance.yahoo.com"
    )

    @property
    def claude_agent_webfetch_hosts(self) -> list[str]:
        return [h.strip().lower() for h in self.CLAUDE_AGENT_WEBFETCH_ALLOWLIST.split(",") if h.strip()]

    @property
    def claude_agent_effective_enabled(self) -> bool:
        """True iff the flag is on AND `claude_agent_sdk` is importable.

        Operators can install the SDK to opt in without flipping any flag;
        deployments that omit the optional dep degrade to 503 with a clear
        message instead of crashing on import.
        """
        if not self.CLAUDE_AGENT_ENABLED:
            return False
        import importlib.util
        return importlib.util.find_spec("claude_agent_sdk") is not None

    # Rate limits
    FINMIND_HOURLY_REQUEST_LIMIT: int = 550  # conservative under registered free-tier 600/hr
    AI_REQUESTS_ANALYST_DAILY: int = 20
    AI_REQUESTS_VIEWER_DAILY: int = 5

    # News sentiment scorer: hard cap on LLM calls per UTC day to bound cost.
    # One call scores up to _BATCH_SIZE (20) headlines, so at the default
    # cap of 100 the daily ceiling is ~2,000 articles — comfortably above
    # FinMind's TW news firehose. Increase for multi-market deployments.
    SENTIMENT_DAILY_LLM_CALL_CAP: int = 100

    # News sentiment scorer: hard cap on LLM calls per UTC day for the
    # *interactive* path that fires inline after a backtest auto-backfill
    # (`news_backfill_service`). Separate budget from the cron's
    # `SENTIMENT_DAILY_LLM_CALL_CAP` so a heavy backtest can't starve the
    # daily cron, and vice versa. Same per-batch math: 100 calls = 2,000
    # articles/day max — for a typical backtest window of ~50-200 articles
    # this lets ~10-40 backtests/day score fully inline before falling
    # through to the cron-grade-it-later path.
    SENTIMENT_INTERACTIVE_BACKFILL_CAP: int = 100

    # News sentiment cron: how many days back the hourly job scans for
    # unscored articles. Default 7 covers the live RSS firehose; crank
    # to 30 / 90 / 365 via the AdminPage RuntimeTunablesCard to drain a
    # fresh FinMind backfill (whose articles' `published_at` would
    # otherwise fall outside the default window and never be picked up).
    SENTIMENT_CRON_MAX_AGE_DAYS: int = 7

    # Discussion: per-persona LLM timeout (seconds). When a single persona's
    # turn doesn't complete in this window we abort it, persist a placeholder,
    # and move on to the next persona — prevents one stuck provider from
    # hanging the whole round indefinitely.
    DISCUSSION_PERSONA_TIMEOUT_SECONDS: int = 60

    # Per-call LLM `max_tokens` budgets. 8192 is generous for non-thinking
    # models (their actual output is far smaller — the cap only ceilings
    # them) and gives reasoning models like MiniMax-M2.7 / DeepSeek-R1
    # ~6-7K headroom for chain-of-thought before the visible JSON / Chinese
    # content lands. Bump via RuntimeTunablesCard if a particularly verbose
    # reasoning model still hits `finish_reason=length` before output.
    SENTIMENT_LLM_MAX_TOKENS: int = 8192
    DISCUSSION_TURN_MAX_TOKENS: int = 8192
    DISCUSSION_SYNTHESIZER_MAX_TOKENS: int = 8192

    # `resolve_key(user_id=None)` (the system-task path used by every
    # background cron) normally consults: system row → .env. For solo /
    # single-admin deployments where the operator has only set per-user
    # keys via the admin UI, falling back to the admin user's per-user
    # key removes the "configure each provider twice" footgun. Disable
    # for multi-tenant deployments where admin keys must be isolated
    # from background workloads.
    SYSTEM_TASK_FALLBACK_TO_ADMIN_KEY: bool = True

    # Admin seed (optional — created on first boot)
    ADMIN_EMAIL: str = ""
    ADMIN_PASSWORD: str = ""

    # Auto-update / GitHub release polling
    GITHUB_OWNER: str = "x812033727"
    GITHUB_REPO: str = "FinceptWeb"
    UPDATE_CHECK_INTERVAL_HOURS: int = 6
    # Argv list run by POST /api/admin/update. Empty = feature disabled
    # (endpoint returns status="not_configured"). Operators set this as a
    # JSON-encoded list of args in .env, e.g.:
    #   UPDATE_COMMAND=["docker","compose","pull"]
    # First token must be in UPDATE_COMMAND_ALLOWLIST. Shell metacharacters
    # are never interpreted (no shell is spawned).
    UPDATE_COMMAND: str = ""
    UPDATE_COMMAND_ALLOWLIST: str = "docker,docker-compose,kubectl,helm,/usr/local/bin/update.sh"

    @property
    def update_command_argv(self) -> list[str]:
        import json
        raw = self.UPDATE_COMMAND.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"UPDATE_COMMAND must be a JSON list of strings: {exc}")
        if not isinstance(parsed, list) or not all(isinstance(p, str) for p in parsed):
            raise ValueError("UPDATE_COMMAND must be a JSON list of strings")
        return parsed

    @property
    def update_command_allowlist(self) -> list[str]:
        return [p.strip() for p in self.UPDATE_COMMAND_ALLOWLIST.split(",") if p.strip()]

    # Prometheus /metrics access control. Default = loopback only. Production
    # operators can either narrow to in-cluster CIDRs or set METRICS_AUTH_TOKEN
    # and have Prometheus pass `Authorization: Bearer <token>`.
    METRICS_ALLOW_CIDRS: str = "127.0.0.1/32,::1/128"
    METRICS_AUTH_TOKEN: str = ""

    @property
    def metrics_allow_cidrs(self) -> list[str]:
        return [c.strip() for c in self.METRICS_ALLOW_CIDRS.split(",") if c.strip()]

    # Environment
    DEBUG: bool = False


settings = Settings()
