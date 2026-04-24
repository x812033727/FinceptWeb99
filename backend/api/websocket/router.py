from fastapi import APIRouter, WebSocket
from api.websocket.manager import handle_market_ws

router = APIRouter()


@router.websocket("/ws/market")
async def market_ws(ws: WebSocket):
    """
    Real-time market price stream.

    Auth flow (client must complete within 5 seconds of connecting):
      1. Connect to ws://host/ws/market
      2. Send: {"action": "auth", "token": "<access_token>"}
      3. Send: {"action": "subscribe", "symbols": ["AAPL", "2330"], "markets": ["US", "TW"]}

    Server messages:
      {"type": "snapshot", "data": {"AAPL:US": {...}, "2330:TW": {...}}}
      {"type": "delta",    "symbol": "AAPL", "market": "US", "data": {...}}
      {"type": "ping",     "ts": "..."}

    Client messages:
      {"action": "auth",        "token": "..."}
      {"action": "subscribe",   "symbols": [...], "markets": [...]}
      {"action": "unsubscribe"}
      {"action": "pong"}
    """
    await handle_market_ws(ws)
