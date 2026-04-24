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
        if not v or v == "change_me":
            if os.environ.get("DEBUG", "").lower() in ("true", "1"):
                return v
            raise ValueError(
                "JWT_SECRET_KEY must be set to a strong random value "
                "(min 32 chars) in production. Set DEBUG=true to bypass."
            )
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
    DEFAULT_LLM_PROVIDER: str = "openai"  # openai | anthropic | gemini | ollama

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

    # Environment
    DEBUG: bool = False


settings = Settings()
