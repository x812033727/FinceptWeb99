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

    # Claude Agent (tool-use via claude-agent-sdk). Off by default; analyst/admin only when on.
    CLAUDE_AGENT_ENABLED: bool = False
    CLAUDE_AGENT_MODEL: str = "claude-sonnet-4-5-20250929"
    CLAUDE_AGENT_MAX_TURNS: int = 8
    CLAUDE_AGENT_TOOL_TIMEOUT_S: int = 30
    CLAUDE_AGENT_PYTHON_MEM_MB: int = 256
    CLAUDE_AGENT_PYTHON_CPU_S: int = 5
    CLAUDE_AGENT_WEBFETCH_ALLOWLIST: str = ""  # comma-separated host allowlist; empty = block

    @property
    def claude_agent_webfetch_hosts(self) -> list[str]:
        return [h.strip().lower() for h in self.CLAUDE_AGENT_WEBFETCH_ALLOWLIST.split(",") if h.strip()]

    # Rate limits
    FINMIND_DAILY_REQUEST_LIMIT: int = 550  # conservative under free-tier 600
    AI_REQUESTS_ANALYST_DAILY: int = 20
    AI_REQUESTS_VIEWER_DAILY: int = 5

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
