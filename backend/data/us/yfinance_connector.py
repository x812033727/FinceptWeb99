"""
yfinance wrapper — sync calls run in a bounded thread executor to avoid
blocking asyncio AND to cap concurrent yfinance HTTP fan-out (each Ticker
call triggers multiple Yahoo HTTP requests).
"""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

# 8 concurrent yfinance fetches keeps Yahoo happy and bounds memory under load.
# Far smaller than the default ThreadPoolExecutor (min(32, cpu*5)) which would
# happily spawn dozens of threads when a screener page hits.
_MAX_WORKERS = 8
_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="yfinance")


def _run(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(_executor, lambda: fn(*args, **kwargs))


def _ts_to_ms(ts) -> int | None:
    """Convert pandas Timestamp or datetime to Unix ms UTC."""
    if ts is None:
        return None
    if hasattr(ts, "timestamp"):
        return int(ts.timestamp() * 1000)
    return None


def _safe_attr(obj, name):
    """fast_info attributes are property descriptors that may trigger HTTP
    calls and raise non-AttributeError exceptions (network, rate limits,
    Yahoo response errors). Plain getattr(..., default) only catches
    AttributeError, so we need a wider net."""
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


async def get_quote(ticker: str) -> dict[str, Any]:
    def _fetch():
        t = yf.Ticker(ticker)
        # fast_info hits Yahoo's chart endpoint (resilient); .info hits the
        # quoteSummary endpoint (Yahoo IP-bans cloud providers there fairly
        # often). Try fast_info first and only call .info defensively — if
        # .info raises we still want the chart-endpoint price.
        fi = t.fast_info
        try:
            info = t.info or {}
        except Exception:
            info = {}
        price = _safe_attr(fi, "last_price") or info.get("currentPrice", 0)
        prev = _safe_attr(fi, "previous_close") or info.get("previousClose", 0)
        open_ = _safe_attr(fi, "open")
        high = _safe_attr(fi, "day_high")
        low = _safe_attr(fi, "day_low")
        volume = _safe_attr(fi, "three_month_average_volume") or info.get("volume", 0)
        market_cap = _safe_attr(fi, "market_cap")

        # Final fallback: fast_info also blocked. Pull last 5d of daily bars
        # via yf.download() (same chart endpoint, different code path) and
        # derive price/prev_close/volume/OHLC from the most recent bar.
        if not price:
            try:
                df = yf.download(
                    tickers=[ticker],
                    period="5d",
                    interval="1d",
                    progress=False,
                    auto_adjust=False,
                )
                if df is not None and not df.empty:
                    df = df.dropna(subset=["Close"])
                    if not df.empty:
                        last = df.iloc[-1]
                        price = float(last["Close"])
                        prev = float(df.iloc[-2]["Close"]) if len(df) >= 2 else price
                        open_ = float(last["Open"]) if not pd.isna(last["Open"]) else open_
                        high = float(last["High"]) if not pd.isna(last["High"]) else high
                        low = float(last["Low"]) if not pd.isna(last["Low"]) else low
                        vol_raw = last.get("Volume")
                        if vol_raw is not None and not pd.isna(vol_raw):
                            volume = int(vol_raw)
            except Exception as exc:
                log.warning("yfinance.quote_download_fallback_failed",
                            extra={"ticker": ticker, "error": str(exc)})

        change = round(price - prev, 4) if prev and price else 0
        change_pct = round(change / prev * 100, 4) if prev else 0
        return {
            "symbol": ticker.upper(),
            "price": price,
            "change": change,
            "change_pct": change_pct,
            "volume": volume,
            "open": open_,
            "high": high,
            "low": low,
            "prev_close": prev,
            "market_cap": market_cap,
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


async def get_batch_quotes(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Batch fetch latest close / change% / volume via yf.download().

    Uses Yahoo's chart endpoint, which is far more rate-limit resilient
    than per-symbol .info calls (Yahoo IP-bans cloud providers on .info
    fairly often). Returns a dict keyed by ticker; missing tickers are
    simply absent from the result.
    """
    if not tickers:
        return {}

    def _fetch() -> dict[str, dict[str, Any]]:
        df = yf.download(
            tickers=tickers,
            period="5d",
            interval="1d",
            group_by="ticker",
            threads=True,
            progress=False,
            auto_adjust=False,
        )
        if df is None or df.empty:
            return {}

        def _row_to_quote(sub: pd.DataFrame) -> dict[str, Any] | None:
            sub = sub.dropna(subset=["Close"])
            if sub.empty:
                return None
            last = sub.iloc[-1]
            close = float(last["Close"])
            prev_close = float(sub.iloc[-2]["Close"]) if len(sub) >= 2 else close
            pct = ((close - prev_close) / prev_close * 100) if prev_close else 0.0
            vol_raw = last.get("Volume")
            vol = int(vol_raw) if vol_raw is not None and not pd.isna(vol_raw) else 0
            return {"price": close, "change_pct": round(pct, 4), "volume": vol}

        out: dict[str, dict[str, Any]] = {}
        if isinstance(df.columns, pd.MultiIndex):
            for t in tickers:
                if t not in df.columns.get_level_values(0):
                    continue
                q = _row_to_quote(df[t])
                if q:
                    out[t] = q
        else:
            q = _row_to_quote(df)
            if q:
                out[tickers[0]] = q
        return out

    try:
        return await _run(_fetch)
    except Exception as exc:
        log.warning("yfinance.batch_quotes_failed", extra={"error": str(exc)})
        return {}


async def get_options(
    ticker: str, expiration_date: str | None = None,
) -> list[dict[str, Any]]:
    """Options chain via yfinance. Pulls calls + puts for one expiry (or all).

    Returned rows match the polygon shape on the keys we care about
    (ticker, contract_type, strike_price, expiration_date) plus market
    data (last_price, volume, open_interest, implied_volatility) so the
    frontend can render either source uniformly.
    """
    def _fetch():
        t = yf.Ticker(ticker)
        try:
            expiries = list(t.options or ())
        except Exception as exc:
            log.warning("yfinance.options.expiries_failed",
                        extra={"ticker": ticker, "error": str(exc)})
            return []

        if not expiries:
            return []

        targets = [expiration_date] if expiration_date else expiries
        out: list[dict[str, Any]] = []
        for exp in targets:
            if exp not in expiries:
                continue
            try:
                chain = t.option_chain(exp)
            except Exception as exc:
                log.warning("yfinance.options.chain_failed",
                            extra={"ticker": ticker, "expiry": exp, "error": str(exc)})
                continue
            for ctype, df in (("call", chain.calls), ("put", chain.puts)):
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    out.append({
                        "ticker": str(row.get("contractSymbol", "")),
                        "underlying_ticker": ticker.upper(),
                        "contract_type": ctype,
                        "expiration_date": exp,
                        "strike_price": float(row["strike"]) if not pd.isna(row.get("strike")) else None,
                        "last_price": float(row["lastPrice"]) if not pd.isna(row.get("lastPrice")) else None,
                        "bid": float(row["bid"]) if not pd.isna(row.get("bid")) else None,
                        "ask": float(row["ask"]) if not pd.isna(row.get("ask")) else None,
                        "volume": int(row["volume"]) if not pd.isna(row.get("volume")) else 0,
                        "open_interest": int(row["openInterest"]) if not pd.isna(row.get("openInterest")) else 0,
                        "implied_volatility": float(row["impliedVolatility"]) if not pd.isna(row.get("impliedVolatility")) else None,
                        "in_the_money": bool(row.get("inTheMoney", False)),
                    })
        return out
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


async def get_calendar(ticker: str) -> dict[str, Any]:
    """Upcoming corporate-event dates from yfinance ``Ticker.calendar``.

    Yahoo's calendar shape for TW listings is shallow but reliable for
    the two events that matter for short-term-trade timing:
      - ``Earnings Date``     — next 法說會 (sometimes a [start, end] range)
      - ``Ex-Dividend Date``  — next 除息日

    Returns a normalised dict so callers don't have to handle
    yfinance's variable shape (DataFrame on some versions, dict on
    others, missing keys on yet others):

        {
          "earnings_date":      "2026-05-12" | None,
          "ex_dividend_date":   "2026-06-15" | None,
        }

    Both values may be None when Yahoo doesn't carry the data — TW
    small-caps are commonly under-covered. Caller should treat None
    as "no scheduled event in the lookahead window".
    """
    def _fetch() -> dict[str, Any]:
        t = yf.Ticker(ticker)
        try:
            cal = t.calendar
        except Exception as exc:
            log.debug("yfinance.calendar_failed",
                      extra={"ticker": ticker, "error": str(exc)})
            return {"earnings_date": None, "ex_dividend_date": None}

        def _coerce_date(v):
            """Yahoo can return a single Timestamp, a list/tuple of two
            (range), or a Python date. Normalise to ISO-date string."""
            if v is None:
                return None
            if isinstance(v, (list, tuple)):
                if not v:
                    return None
                v = v[0]
            try:
                if hasattr(v, "strftime"):
                    return v.strftime("%Y-%m-%d")
                if isinstance(v, str):
                    return v[:10]
            except Exception:
                return None
            return None

        # Pre-2024 yfinance returns a pandas DataFrame; current returns
        # a plain dict. Normalise to dict access.
        if isinstance(cal, pd.DataFrame):
            if cal.empty:
                return {"earnings_date": None, "ex_dividend_date": None}
            # DataFrame columns are dates, index is the field name.
            try:
                cal_dict = {idx: cal.loc[idx].iloc[0] for idx in cal.index}
            except Exception:
                cal_dict = {}
        elif isinstance(cal, dict):
            cal_dict = cal
        else:
            cal_dict = {}

        return {
            "earnings_date":    _coerce_date(cal_dict.get("Earnings Date")),
            "ex_dividend_date": _coerce_date(cal_dict.get("Ex-Dividend Date")),
        }

    return await _run(_fetch)
