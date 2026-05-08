"""
Tool catalogue exposed to Claude Agent SDK.

Every tool that reads user data is built inside `build_toolset` so it can
close over `user_id` and the db session factory — no module-level tool
ever sees free-form user identity. The return value is an MCP server
config suitable for `ClaudeAgentOptions(mcp_servers={...})`.
"""
from datetime import date

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server

from .financial import make_financial_tools
from .sql import make_sql_tools
from .web import make_web_tools
from .python_exec import make_python_tools


def build_toolset(
    user_id: str,
    *,
    as_of_date: date | None = None,
) -> McpSdkServerConfig:
    """Compose per-user toolset. All user-scoped tools close over user_id.

    `as_of_date` (backtest mode): forwarded to `make_financial_tools`
    so the 4 TW chip-flow tools anchor at the historical date instead
    of `today` for backtest discussions. The LLM doesn't see `as_of`
    in any tool's parameters — backtest correctness is enforced at the
    closure layer.
    """
    tools = [
        *make_financial_tools(as_of_date=as_of_date),
        *make_sql_tools(user_id),
        *make_web_tools(),
        *make_python_tools(),
    ]
    return create_sdk_mcp_server("fincept", "1.0.0", tools=tools)


def tool_names() -> list[str]:
    """Return the fully-qualified allowed_tools list for ClaudeAgentOptions."""
    return [
        "mcp__fincept__get_quote",
        "mcp__fincept__run_dcf",
        "mcp__fincept__run_var",
        "mcp__fincept__run_backtest",
        "mcp__fincept__query_user_data",
        "mcp__fincept__get_options_chain",
        "mcp__fincept__get_symbol_news",
        "mcp__fincept__get_symbol_sentiment",
        "mcp__fincept__get_peers",
        "mcp__fincept__get_financials",
        "mcp__fincept__get_institutional_history",
        "mcp__fincept__get_margin_history",
        "mcp__fincept__get_top_brokers",
        "mcp__fincept__get_taifex_positioning",
        "mcp__fincept__web_fetch",
        "mcp__fincept__python_exec",
    ]
