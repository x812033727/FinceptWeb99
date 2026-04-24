"""
Read-only query over the caller's own data. Not a free-form SQL tool —
`resource` is a whitelisted enum and every query ends with a forced
user_id predicate that the model cannot override.
"""
import json
import logging
import uuid
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.session import AsyncSessionLocal
from models.alert import PriceAlert
from models.portfolio import Holding, Portfolio, Transaction
from models.watchlist import Watchlist, WatchlistItem

logger = logging.getLogger(__name__)

# Resource → (model, default columns surfaced to the model)
_RESOURCES = {
    "portfolios": (Portfolio, ["id", "name", "currency", "created_at"]),
    "holdings":   (Holding,   ["portfolio_id", "symbol", "market", "quantity", "avg_cost"]),
    "transactions": (Transaction, ["id", "portfolio_id", "symbol", "market", "tx_type", "quantity", "price", "executed_at"]),
    "watchlists": (Watchlist, ["id", "name", "created_at"]),
    "watchlist_items": (WatchlistItem, ["watchlist_id", "symbol", "market", "added_at"]),
    "alerts":     (PriceAlert, ["id", "symbol", "market", "condition", "target_price", "triggered", "created_at"]),
}


def _row_to_dict(row: Any, cols: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in cols:
        v = getattr(row, c, None)
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        elif hasattr(v, "value"):   # enum
            v = v.value
        elif isinstance(v, uuid.UUID):
            v = str(v)
        out[c] = v
    return out


def make_sql_tools(user_id: str) -> list[SdkMcpTool]:
    """Build the user-scoped SQL tool. `user_id` is baked into the closure."""
    uid = uuid.UUID(user_id)

    @tool(
        "query_user_data",
        "Read-only access to the caller's own data. "
        f"resource must be one of: {sorted(_RESOURCES.keys())}. "
        "`limit` caps rows at 100. "
        "Holdings/transactions/watchlist_items are always scoped to the calling user's parent records.",
        {"resource": str, "limit": int},
    )
    async def query_user_data(args: dict[str, Any]) -> dict:
        resource = args.get("resource", "").lower()
        limit = min(int(args.get("limit", 50)), 100)
        if resource not in _RESOURCES:
            return {"content": [{"type": "text",
                "text": json.dumps({"error": f"Unknown resource. Allowed: {sorted(_RESOURCES.keys())}"})}]}

        model, cols = _RESOURCES[resource]
        async with AsyncSessionLocal() as session:
            try:
                stmt = select(model).limit(limit)
                # Force user scoping — direct vs join via parent
                if hasattr(model, "user_id"):
                    stmt = stmt.where(model.user_id == uid)
                elif model is Holding or model is Transaction:
                    stmt = stmt.join(Portfolio).where(Portfolio.user_id == uid)
                elif model is WatchlistItem:
                    stmt = stmt.join(Watchlist).where(Watchlist.user_id == uid)
                else:
                    # If we ever add a resource we don't know how to scope, refuse.
                    return {"content": [{"type": "text",
                        "text": json.dumps({"error": "Resource cannot be user-scoped safely"})}]}

                if model is Watchlist:
                    stmt = stmt.options(selectinload(Watchlist.items))

                rows = (await session.execute(stmt)).scalars().all()
                payload = {
                    "resource": resource,
                    "count": len(rows),
                    "rows": [_row_to_dict(r, cols) for r in rows],
                }
                return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}
            except Exception as exc:
                logger.warning("query_user_data tool failed for user=%s resource=%s: %s",
                               user_id, resource, exc)
                return {"content": [{"type": "text",
                    "text": json.dumps({"error": str(exc)})}]}

    return [query_user_data]
