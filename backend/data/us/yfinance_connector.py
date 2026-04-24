"""
yfinance wrapper — sync calls run in a thread executor to avoid blocking asyncio.
All timestamps normalized to Unix ms UTC before returning.
"""
import asyncio
from datetime import datetime, timezone
from typing import Any
import yfinance as yf


def _run(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, lambda: fn(*args, **kwargs))


def _ts_to_ms(ts) -> int | None:
    """Convert pandas Timestamp or datetime to Unix ms UTC."""
    if ts is None:
        return None
    if hasattr(ts, "timestamp"):
        return int(ts.timestamp() * 1000)
    return None


async def get_quote(ticker: str) -> dict[str, Any]:
    def _fetch():
        t = yf.Ticker(ticker)
        fi = t.fast_info
        info = t.info or {}
        price = getattr(fi, "last_price", None) or info.get("currentPrice", 0)
        prev = getattr(fi, "previous_close", None) or info.get("previousClose", 0)
        change = round(price - prev, 4) if prev else 0
        change_pct = round(change / prev * 100, 4) if prev else 0
        return {
            "symbol": ticker.upper(),
            "price": price,
            "change": change,
            "change_pct": change_pct,
            "volume": getattr(fi, "three_month_average_volume", None) or info.get("volume", 0),
            "open": getattr(fi, "open", None),
            "high": getattr(fi, "day_high", None),
            "low": getattr(fi, "day_low", None),
            "prev_close": prev,
            "market_cap": getattr(fi, "market_cap", None),
            "ts": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
    return await _run(_fetch)


async def get_history(
    ticker: str,
    period: str = "1y",
    interval: str = "1d",
) -> list[dict[str, Any]]:
    def _fetch():
        df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
        if df.empty:
            return []
        bars = []
        for ts, row in df.iterrows():
            bars.append({
                "time": _ts_to_ms(ts),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": int(row["Volume"]),
            })
        return bars
    return await _run(_fetch)


async def get_info(ticker: str) -> dict[str, Any]:
    def _fetch():
        return yf.Ticker(ticker).info or {}
    return await _run(_fetch)


async def get_financials(ticker: str) -> dict[str, Any]:
    def _fetch():
        t = yf.Ticker(ticker)
        def _df_to_list(df):
            if df is None or df.empty:
                return []
            df = df.T.reset_index()
            df.columns = [str(c) for c in df.columns]
            return df.to_dict(orient="records")
        return {
            "income_statement": _df_to_list(t.financials),
            "balance_sheet": _df_to_list(t.balance_sheet),
            "cash_flow": _df_to_list(t.cashflow),
        }
    return await _run(_fetch)
