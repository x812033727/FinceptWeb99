"""Post-mortem review for backtest discussions.

After a backtest discussion concludes, this service computes the
**actual top-N gainers** on the next trading day after ``as_of_date``
and injects a self-critique prompt into the discussion's transcript.
The next round of personas then has to defend or revise their
conclusion against ground truth.

Why this matters: a backtest with no ground-truth feedback is just
a pretty hallucination. The personas can't learn what they missed
unless we shove the actual outcome under their nose. Calling out
specific top gainers + asking "why did you ignore X" forces the
model to either:

  - Find the signal in the prior ctx that pointed to it (and admit
    they discounted it incorrectly), or
  - Identify what data was missing and would have helped them
    catch it.

Both outcomes are useful. The second one tells the operator which
new context blocks would actually move analyst quality forward.

Read-only against ``ohlcv_daily``; writes a single ``user_input``
turn via the existing ``inject_user_message`` plumbing. No new
table, no migration.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion
from models.ohlcv_daily import OhlcvDaily

log = logging.getLogger(__name__)


# Cap the lookahead — if there's no trading day within this many
# calendar days of as_of, we surface "no data" rather than walking
# forever. 14 days covers Lunar New Year + a public holiday cluster.
_MAX_LOOKAHEAD_DAYS = 14
_DEFAULT_TOP_N = 5


@dataclass(frozen=True)
class GainerRow:
    symbol: str
    change_pct: float
    close: float
    base_close: float
    next_trading_day: date


async def _resolve_next_trading_day(
    db: AsyncSession, *, market: str, as_of: date,
) -> date | None:
    """Smallest ``ts > as_of`` actually present in ``ohlcv_daily`` for
    `market`. Naturally skips weekends + holidays — we don't need a
    separate calendar table.
    """
    stmt = (
        select(OhlcvDaily.ts)
        .where(
            OhlcvDaily.market == market,
            OhlcvDaily.ts > as_of,
            OhlcvDaily.ts <= date.fromordinal(
                as_of.toordinal() + _MAX_LOOKAHEAD_DAYS,
            ),
        )
        .order_by(OhlcvDaily.ts.asc())
        .limit(1)
    )
    return await db.scalar(stmt)


async def compute_top_gainers_after(
    db: AsyncSession,
    *,
    as_of: date,
    market: str = "TW",
    n: int = _DEFAULT_TOP_N,
) -> tuple[date | None, list[GainerRow]]:
    """Return ``(next_trading_day, top_n_gainers)``.

    The change% is computed close-on-next-day vs close-on-as_of for
    each symbol that has both bars. Symbols with a missing
    base-close (NULL or 0) are dropped — division-by-zero protection
    plus filtering out fresh listings whose first close lacks a
    legitimate prior-day baseline.

    Synthetic index symbols (``_TAIEX``, ``_TAIEX_TR``) are excluded
    so the leaderboard only carries tradeable equities.
    """
    next_day = await _resolve_next_trading_day(db, market=market, as_of=as_of)
    if next_day is None:
        return None, []

    # Pull base + next-day closes for every symbol that has both.
    base_stmt = (
        select(OhlcvDaily.symbol, OhlcvDaily.close)
        .where(OhlcvDaily.market == market, OhlcvDaily.ts == as_of)
    )
    next_stmt = (
        select(OhlcvDaily.symbol, OhlcvDaily.close)
        .where(OhlcvDaily.market == market, OhlcvDaily.ts == next_day)
    )
    base_closes = {
        row[0]: float(row[1]) if row[1] is not None else None
        for row in (await db.execute(base_stmt)).all()
    }
    next_closes = {
        row[0]: float(row[1]) if row[1] is not None else None
        for row in (await db.execute(next_stmt)).all()
    }

    gainers: list[GainerRow] = []
    for sym, next_close in next_closes.items():
        if sym.startswith("_"):
            continue   # exclude synthetic index symbols
        base_close = base_closes.get(sym)
        if base_close in (None, 0) or next_close is None:
            continue
        change_pct = (next_close / base_close - 1) * 100
        gainers.append(GainerRow(
            symbol=sym,
            change_pct=round(change_pct, 4),
            close=next_close,
            base_close=base_close,
            next_trading_day=next_day,
        ))

    gainers.sort(key=lambda g: g.change_pct, reverse=True)
    return next_day, gainers[:n]


def format_post_mortem_prompt(
    *,
    as_of: date,
    next_trading_day: date,
    top_gainers: list[GainerRow],
    recommended_symbols: list[str],
    company_name_lookup: dict[str, str | None] | None = None,
) -> str:
    """Build the Traditional-Chinese self-critique message that goes
    into the user_input turn. Lays out:

      1. Recap of what the personas had recommended
      2. The actual top-N gainers on next_trading_day
      3. A four-question structured prompt forcing concrete review
    """
    name_lookup = company_name_lookup or {}
    lines: list[str] = []
    lines.append("【事後檢討 — 對答案】")
    lines.append("")
    lines.append(
        f"這場討論的回測日期是 **{as_of.isoformat()}**。"
        f"隔一個交易日 **{next_trading_day.isoformat()}** 的"
        f"實際漲幅前 {len(top_gainers)} 名為："
    )
    lines.append("")
    for i, g in enumerate(top_gainers, start=1):
        nm = name_lookup.get(g.symbol)
        label = f"{g.symbol} ({nm})" if nm else g.symbol
        sign = "+" if g.change_pct >= 0 else ""
        lines.append(
            f"{i}. **{label}**　{sign}{g.change_pct:.2f}%"
        )
    lines.append("")
    if recommended_symbols:
        lines.append(
            f"你們先前的結論推薦了：{', '.join(recommended_symbols)}。"
        )
    else:
        lines.append("你們先前的結論沒有推薦任何具體標的。")
    lines.append("")
    lines.append("請各位專家依以下四點誠實檢討（**不要 defensive**）：")
    lines.append("")
    lines.append(
        "1. **命中與否**：你的推薦有進入前 5 嗎？"
        "如果有，當時是哪個訊號讓你看對的？"
    )
    lines.append(
        "2. **漏掉的標的**：對於沒命中的前 5 名，"
        "回頭看 round 1 的 ctx 區塊（focus_briefs / news_sentiment / "
        "外資台指期 / industry_rs / day_trading_trend / 借券 / "
        "upcoming_event 等），有哪些徵兆其實已存在但被你忽略？"
        "請具體點名是哪一筆數據。"
    )
    lines.append(
        "3. **誤判方向的訊號**：哪些訊號讓你誤推薦了沒漲的標的？"
        "是訊號本身有缺陷，還是你的權重給錯？"
    )
    lines.append(
        "4. **缺失的資料**：要正確預測這幾檔，"
        "目前 ctx 還缺哪一類資料？建議下次加入什麼新訊號？"
    )
    lines.append("")
    lines.append(
        "請聚焦在你**自己**的判斷檢討，不要泛泛而論大盤。"
        "回答完後本輪結束，使用者會再彙整一次最終結論。"
    )
    return "\n".join(lines)


async def build_post_mortem_message(
    db: AsyncSession,
    discussion: Discussion,
    *,
    n: int = _DEFAULT_TOP_N,
) -> tuple[date | None, list[GainerRow], str]:
    """High-level helper: compute top gainers + format the prompt.

    Returns ``(next_trading_day, gainers, prompt_text)``. Caller
    should treat ``next_trading_day=None`` as "no data available"
    and surface a 400 to the user (e.g. as_of is today / future,
    or the archive doesn't reach this date yet).
    """
    if discussion.as_of_date is None:
        raise ValueError(
            "post_mortem requires a backtest discussion (as_of_date is null)"
        )
    next_day, gainers = await compute_top_gainers_after(
        db, as_of=discussion.as_of_date, market=discussion.market, n=n,
    )
    if next_day is None or not gainers:
        return next_day, gainers, ""

    # Best-effort: enrich symbol → 公司簡稱 from the in-memory map
    # populated by the daily `tw_symbol_map` cron. None when the
    # symbol isn't in the map; format_post_mortem_prompt handles
    # that gracefully.
    name_lookup: dict[str, str | None] = {}
    if discussion.market == "TW":
        try:
            from services.tw_market_service import get_company_name
            for g in gainers:
                name_lookup[g.symbol] = get_company_name(g.symbol)
        except Exception as exc:
            log.debug("post_mortem.name_lookup_failed",
                      extra={"error": str(exc)})

    recommended = []
    if discussion.conclusion:
        raw = discussion.conclusion.get("recommended_symbols") or []
        recommended = [str(s) for s in raw if s]

    text = format_post_mortem_prompt(
        as_of=discussion.as_of_date,
        next_trading_day=next_day,
        top_gainers=gainers,
        recommended_symbols=recommended,
        company_name_lookup=name_lookup,
    )
    return next_day, gainers, text


def gainer_row_to_dict(g: GainerRow) -> dict[str, Any]:
    """Serialise for API responses + tests."""
    return {
        "symbol":             g.symbol,
        "change_pct":         g.change_pct,
        "close":              g.close,
        "base_close":         g.base_close,
        "next_trading_day":   g.next_trading_day.isoformat(),
    }
