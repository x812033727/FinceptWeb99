"""Post-mortem review for backtest discussions.

After a backtest discussion concludes, this service computes two
ground-truth views over the next 5 trading days after
``as_of_date`` and injects a structured self-critique prompt:

  1. **Recommended symbols' own performance** (PR #273) — for
     each symbol the personas recommended, the D1-D5 close
     change %s vs ``as_of_date``. Lets personas judge whether
     their picks actually delivered, not just whether they
     happened to overlap with random one-day winners.

  2. **Each day's top-N gainers** (PR #273 evolved from PR #249) —
     the actual top-N gainers for each of D1, D2, ..., D5
     individually, so personas can spot symbols that won across
     multiple days (high-conviction missed picks) vs single-day
     spikes.

Why both: a backtest with no ground-truth feedback is pretty
hallucination. The earlier "next-day top 5" alone over-weighted
single-day noise — a one-day spike that round-tripped by D5
isn't a signal worth chasing. Comparing the picks against
themselves over a 5-day window AND showing what was actually
trending across those 5 days gives the richest critique surface.

Read-only against ``ohlcv_daily``; writes a single ``user_input``
turn via the existing ``inject_user_message`` plumbing. No new
table, no migration.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.discussion import Discussion
from models.ohlcv_daily import OhlcvDaily

log = logging.getLogger(__name__)


# Cap the lookahead — if there's no Nth trading day within this
# many calendar days of as_of, we surface partial / no data
# rather than walking forever. 21 days covers Lunar New Year +
# a public holiday cluster + a 5-trading-day window.
_MAX_LOOKAHEAD_DAYS = 21
_DEFAULT_TOP_N = 5
_DEFAULT_DAYS = 5


OutcomeStatus = Literal["win", "miss", "insufficient_data"]


# ── Data structures ──────────────────────────────────────────────


@dataclass(frozen=True)
class DayPerformance:
    """One symbol's close + change % on a single trading day."""
    trading_day: date
    close: float
    change_pct: float           # vs the recommendation's base_close


@dataclass(frozen=True)
class RecommendedPerformance:
    """Per-symbol D1-D5 self-evaluation row."""
    symbol: str
    base_close: float           # close on as_of_date (the entry price)
    days: list[DayPerformance]   # length up to `_DEFAULT_DAYS`


@dataclass(frozen=True)
class GainerRow:
    """One row in a daily top-N leaderboard."""
    symbol: str
    change_pct: float           # vs the previous trading day's close
    close: float
    base_close: float
    trading_day: date


@dataclass(frozen=True)
class DailyGainers:
    """All top-N gainers for a single trading day in the window."""
    trading_day: date
    gainers: list[GainerRow]


@dataclass(frozen=True)
class WinnerEntry:
    """One recommended symbol that hit the win threshold."""
    symbol: str
    peak_pct: float        # max cumulative change % across the window
    peak_day: date         # which trading day the peak landed on


@dataclass(frozen=True)
class OutcomeVerdict:
    """Result of `evaluate_recommendation_outcome`. Drives whether the
    post-mortem self-critique runs at all (skip on `win`)."""
    status: OutcomeStatus
    threshold_pct: float
    window_days: int
    winners: list[WinnerEntry]
    best_pct: float | None
    reason: str


@dataclass
class PostMortemPayload:
    """Everything `build_post_mortem_message` returns. Keeps the
    caller surface clean — one dataclass instead of a 5-tuple."""
    trading_days: list[date] = field(default_factory=list)
    recommended_performance: list[RecommendedPerformance] = field(
        default_factory=list,
    )
    daily_top_gainers: list[DailyGainers] = field(default_factory=list)
    prompt_text: str = ""
    verdict: OutcomeVerdict | None = None


# ── Trading-day resolution ───────────────────────────────────────


async def _resolve_trading_days_after(
    db: AsyncSession, *, market: str, as_of: date, days: int,
) -> list[date]:
    """Return the next `days` trading days strictly after `as_of`,
    derived from rows actually present in `ohlcv_daily` for
    `market`. Naturally skips weekends + holidays (no separate
    calendar table needed). Returns the partial list when the
    archive doesn't reach `days` ahead — caller decides whether
    to bail or render what it has.
    """
    stmt = (
        select(OhlcvDaily.ts)
        .where(
            OhlcvDaily.market == market,
            OhlcvDaily.ts > as_of,
            OhlcvDaily.ts <= as_of + timedelta(days=_MAX_LOOKAHEAD_DAYS),
        )
        .group_by(OhlcvDaily.ts)
        .order_by(OhlcvDaily.ts.asc())
        .limit(days)
    )
    rows = (await db.scalars(stmt)).all()
    return [r for r in rows]


# ── Recommended symbols' D1-D5 performance ───────────────────────


async def compute_recommended_performance(
    db: AsyncSession,
    *,
    as_of: date,
    market: str,
    recommended_symbols: list[str],
    trading_days: list[date],
) -> list[RecommendedPerformance]:
    """Per-symbol D1-D5 close changes vs the as_of close.

    Empty `recommended_symbols` returns []. Symbols missing the
    base bar (not listed yet on as_of) are skipped silently —
    can't compute change without an entry price.
    """
    if not recommended_symbols or not trading_days:
        return []
    syms = [s for s in recommended_symbols if s and not s.startswith("_")]
    if not syms:
        return []

    base_stmt = (
        select(OhlcvDaily.symbol, OhlcvDaily.close)
        .where(
            OhlcvDaily.market == market,
            OhlcvDaily.ts == as_of,
            OhlcvDaily.symbol.in_(syms),
        )
    )
    base_closes = {
        row[0]: float(row[1]) if row[1] is not None else None
        for row in (await db.execute(base_stmt)).all()
    }

    day_stmt = (
        select(OhlcvDaily.symbol, OhlcvDaily.ts, OhlcvDaily.close)
        .where(
            OhlcvDaily.market == market,
            OhlcvDaily.ts.in_(trading_days),
            OhlcvDaily.symbol.in_(syms),
        )
    )
    by_symbol: dict[str, dict[date, float]] = {}
    for row in (await db.execute(day_stmt)).all():
        sym, ts, close = row[0], row[1], row[2]
        if close is None:
            continue
        by_symbol.setdefault(sym, {})[ts] = float(close)

    out: list[RecommendedPerformance] = []
    for sym in syms:
        base_close = base_closes.get(sym)
        if base_close in (None, 0):
            continue
        day_map = by_symbol.get(sym, {})
        days_perf: list[DayPerformance] = []
        for d in trading_days:
            close = day_map.get(d)
            if close is None:
                continue
            change_pct = (close / base_close - 1) * 100
            days_perf.append(DayPerformance(
                trading_day=d,
                close=close,
                change_pct=round(change_pct, 4),
            ))
        if not days_perf:
            continue
        out.append(RecommendedPerformance(
            symbol=sym,
            base_close=base_close,
            days=days_perf,
        ))
    return out


# ── Daily top-N gainers across the window ────────────────────────


async def compute_daily_top_gainers(
    db: AsyncSession,
    *,
    market: str,
    trading_days: list[date],
    n: int,
) -> list[DailyGainers]:
    """For each day in `trading_days`, compute the top-N gainers
    measured as that day's close vs the previous trading day's
    close. Uses each day's true previous bar (D2's base is D1's
    close, etc.) so the leaderboards reflect single-day momentum
    rather than cumulative since `as_of`.
    """
    if not trading_days:
        return []

    # Find the day BEFORE the first trading day so we can compute
    # change for the first day too. Look back up to
    # _MAX_LOOKAHEAD_DAYS calendar days for the closest earlier bar.
    first_day = trading_days[0]
    prev_stmt = (
        select(OhlcvDaily.ts)
        .where(
            OhlcvDaily.market == market,
            OhlcvDaily.ts < first_day,
            OhlcvDaily.ts >= first_day - timedelta(days=_MAX_LOOKAHEAD_DAYS),
        )
        .group_by(OhlcvDaily.ts)
        .order_by(OhlcvDaily.ts.desc())
        .limit(1)
    )
    prev_to_first = await db.scalar(prev_stmt)
    all_days_for_query = (
        ([prev_to_first] if prev_to_first else []) + list(trading_days)
    )

    # One scan: pull every (symbol, ts, close) for the relevant days.
    stmt = (
        select(OhlcvDaily.symbol, OhlcvDaily.ts, OhlcvDaily.close)
        .where(
            OhlcvDaily.market == market,
            OhlcvDaily.ts.in_(all_days_for_query),
        )
    )
    closes_by_day: dict[date, dict[str, float]] = {d: {} for d in all_days_for_query}
    for row in (await db.execute(stmt)).all():
        sym, ts, close = row[0], row[1], row[2]
        if close is None or sym.startswith("_"):
            continue
        closes_by_day.setdefault(ts, {})[sym] = float(close)

    out: list[DailyGainers] = []
    prev_day = prev_to_first
    for d in trading_days:
        if prev_day is None:
            # No baseline — can't compute change for this first day.
            out.append(DailyGainers(trading_day=d, gainers=[]))
            prev_day = d
            continue
        today_closes = closes_by_day.get(d, {})
        prev_closes = closes_by_day.get(prev_day, {})
        gainers: list[GainerRow] = []
        for sym, today_close in today_closes.items():
            base_close = prev_closes.get(sym)
            if base_close in (None, 0):
                continue
            change_pct = (today_close / base_close - 1) * 100
            gainers.append(GainerRow(
                symbol=sym,
                change_pct=round(change_pct, 4),
                close=today_close,
                base_close=base_close,
                trading_day=d,
            ))
        gainers.sort(key=lambda g: g.change_pct, reverse=True)
        out.append(DailyGainers(trading_day=d, gainers=gainers[:n]))
        prev_day = d
    return out


# ── Prompt formatter ─────────────────────────────────────────────


def _format_recommended_table(
    rows: list[RecommendedPerformance],
    trading_days: list[date],
    name_lookup: dict[str, str | None],
) -> list[str]:
    """Pipe-table rendering of per-symbol D1-D5 perf. Falls back to
    `(無資料)` for missing day cells."""
    if not rows:
        return ["（沒有推薦標的可比對，或標的均無 D1-D5 ohlcv 資料）"]
    header = ["標的", "基準收盤"] + [
        f"D{i+1} ({d.isoformat()})" for i, d in enumerate(trading_days)
    ]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * len(header)) + " |",
    ]
    for r in rows:
        nm = name_lookup.get(r.symbol)
        label = f"{r.symbol} ({nm})" if nm else r.symbol
        cells = [label, f"{r.base_close:g}"]
        day_map = {dp.trading_day: dp for dp in r.days}
        for d in trading_days:
            dp = day_map.get(d)
            if dp is None:
                cells.append("(無資料)")
            else:
                sign = "+" if dp.change_pct >= 0 else ""
                cells.append(f"{sign}{dp.change_pct:.2f}%")
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _format_daily_gainers(
    daily: list[DailyGainers],
    name_lookup: dict[str, str | None],
) -> list[str]:
    """Each day rendered as a numbered list of gainers."""
    lines: list[str] = []
    for i, day_block in enumerate(daily, start=1):
        if not day_block.gainers:
            lines.append(
                f"**D{i} ({day_block.trading_day.isoformat()})**：（當日無 ohlcv 資料）"
            )
            lines.append("")
            continue
        lines.append(
            f"**D{i} ({day_block.trading_day.isoformat()}) 漲幅前 "
            f"{len(day_block.gainers)} 名：**"
        )
        for j, g in enumerate(day_block.gainers, start=1):
            nm = name_lookup.get(g.symbol)
            label = f"{g.symbol} ({nm})" if nm else g.symbol
            sign = "+" if g.change_pct >= 0 else ""
            lines.append(f"  {j}. **{label}**　{sign}{g.change_pct:.2f}%")
        lines.append("")
    return lines


def evaluate_recommendation_outcome(
    rec_perf: list[RecommendedPerformance],
    *,
    threshold_pct: float,
    window_days: int,
    trading_days: list[date],
) -> OutcomeVerdict:
    """Pure function: derive a `win` / `miss` / `insufficient_data`
    verdict from the per-recommendation D1-D{window} table.

    `win`  — at least one symbol's peak cumulative change % within
             the (truncated to `window_days`) window meets `threshold_pct`.
    `miss` — every recommendation's peak fell short.
    `insufficient_data` — no trading days resolved at all (archive
             didn't reach as_of+1) or no recommended symbols had
             entry-day bars.
    """
    if not trading_days:
        return OutcomeVerdict(
            status="insufficient_data",
            threshold_pct=threshold_pct,
            window_days=window_days,
            winners=[],
            best_pct=None,
            reason="no trading days resolved past as_of",
        )
    window = trading_days[:window_days]
    if not rec_perf:
        return OutcomeVerdict(
            status="insufficient_data",
            threshold_pct=threshold_pct,
            window_days=window_days,
            winners=[],
            best_pct=None,
            reason="no recommended symbols had entry-day bars",
        )

    best_pct: float | None = None
    winners: list[WinnerEntry] = []
    for r in rec_perf:
        peak_in_window: tuple[float, date] | None = None
        for dp in r.days:
            if dp.trading_day not in window:
                continue
            if peak_in_window is None or dp.change_pct > peak_in_window[0]:
                peak_in_window = (dp.change_pct, dp.trading_day)
        if peak_in_window is None:
            continue
        peak_pct, peak_day = peak_in_window
        if best_pct is None or peak_pct > best_pct:
            best_pct = peak_pct
        if peak_pct >= threshold_pct:
            winners.append(WinnerEntry(
                symbol=r.symbol,
                peak_pct=round(peak_pct, 4),
                peak_day=peak_day,
            ))

    if winners:
        return OutcomeVerdict(
            status="win",
            threshold_pct=threshold_pct,
            window_days=window_days,
            winners=sorted(winners, key=lambda w: w.peak_pct, reverse=True),
            best_pct=round(best_pct, 4) if best_pct is not None else None,
            reason=(
                f"{len(winners)} symbol(s) reached >= {threshold_pct:g}% "
                f"within {window_days} trading days"
            ),
        )
    return OutcomeVerdict(
        status="miss",
        threshold_pct=threshold_pct,
        window_days=window_days,
        winners=[],
        best_pct=round(best_pct, 4) if best_pct is not None else None,
        reason=(
            f"all recommendations peaked below {threshold_pct:g}% "
            f"within {window_days} trading days"
        ),
    )


def format_post_mortem_miss_prompt(
    *,
    as_of: date,
    trading_days: list[date],
    daily_top_gainers: list[DailyGainers],
    recommended_symbols: list[str],
    verdict: OutcomeVerdict,
    company_name_lookup: dict[str, str | None] | None = None,
) -> str:
    """Miss-branch prompt: omit the self-eval section entirely. The
    recommendation already lost, asking personas to re-grade their own
    picks adds noise; we want them focused on which signals would have
    surfaced the actual winners.
    """
    name_lookup = company_name_lookup or {}
    lines: list[str] = ["【事後檢討 — 漲幅榜複盤】", ""]

    last_day_label = (
        f"D{len(trading_days)} ({trading_days[-1].isoformat()})"
        if trading_days else "（無資料）"
    )
    lines.append(
        f"回測日期 **{as_of.isoformat()}**，"
        f"評估窗口 **D1 ({trading_days[0].isoformat()}) ~ {last_day_label}**。"
        if trading_days
        else f"回測日期 **{as_of.isoformat()}**，但評估窗口的 ohlcv 尚未抵達。"
    )
    lines.append("")

    best = (
        f"{verdict.best_pct:+.2f}%"
        if verdict.best_pct is not None else "（無資料）"
    )
    lines.append(
        f"❌ 你的推薦 {', '.join(recommended_symbols) or '（無）'} 在 "
        f"{verdict.window_days} 個交易日內最高僅 {best}，"
        f"未達 {verdict.threshold_pct:g}% 門檻。"
    )
    lines.append("")

    lines.append("## 真正的贏家（你錯過了誰）")
    lines.append("")
    lines.extend(_format_daily_gainers(daily_top_gainers, name_lookup))

    lines.append("## 反思（聚焦在「漏看」，不要 defensive）")
    lines.append("")
    lines.append(
        "1. **缺漏的主題**：上面榜單上的贏家是否屬於你當初忽略的產業 / 主題？"
        "請點出共通的 sector 或 catalyst。"
    )
    lines.append(
        "2. **回頭翻 ctx**：回頭看 round 1 的 ctx 區塊"
        "（focus_briefs / news_sentiment / 外資台指期 / industry_rs / "
        "day_trading_trend / 借券 / overseas_indicators / "
        "single_stock_futures_oi / upcoming_event 等），"
        "哪些徵兆其實已存在但被你忽略？請具體點名是哪一筆數據。"
    )
    lines.append(
        "3. **訊號權重檢討**：哪些訊號 round 1 給太高權重、結果是雜訊？"
        "哪些反過來太低權重、其實是真訊號？"
    )
    lines.append(
        "4. **缺失的資料**：要正確預測這些動能標的，"
        "目前 ctx 還缺哪一類資料？建議下次加入什麼新訊號？"
    )
    lines.append("")
    lines.append(
        "請聚焦在「為什麼當時沒看到」，不要重複自評你的推薦。"
        "回答完後本輪結束，使用者會再彙整一次最終結論。"
    )
    return "\n".join(lines)


def format_post_mortem_prompt(
    *,
    as_of: date,
    trading_days: list[date],
    recommended_performance: list[RecommendedPerformance],
    daily_top_gainers: list[DailyGainers],
    recommended_symbols: list[str],
    company_name_lookup: dict[str, str | None] | None = None,
) -> str:
    """Build the Traditional-Chinese self-critique message.

    Sections (PR #273):
      1. Recap of recommendation + evaluation window
      2. Per-recommendation D1-D5 self-evaluation table
         (跟自己比 — did the picks actually deliver)
      3. Per-day top-N gainers across D1-D5
         (重複出現的標的 = 高確信 missed picks)
      4. Four reflection questions tying the two views together
    """
    name_lookup = company_name_lookup or {}
    lines: list[str] = ["【事後檢討 — 對答案】", ""]

    last_day_label = (
        f"D{len(trading_days)} ({trading_days[-1].isoformat()})"
        if trading_days
        else "（無資料）"
    )
    lines.append(
        f"回測日期 **{as_of.isoformat()}**，"
        f"評估窗口 **D1 ({trading_days[0].isoformat()}) ~ {last_day_label}**。"
        if trading_days
        else f"回測日期 **{as_of.isoformat()}**，但評估窗口的 ohlcv 尚未抵達。"
    )
    lines.append("")

    # Section 1 — recap.
    if recommended_symbols:
        lines.append(f"## 你們先前的推薦：{', '.join(recommended_symbols)}")
    else:
        lines.append("## 你們先前的結論沒有推薦任何具體標的")
    lines.append("")

    # Section 2 — self-eval.
    lines.append("## A. 你的推薦 D1-D5 自評（跟自己比）")
    lines.append("")
    lines.extend(_format_recommended_table(
        recommended_performance, trading_days, name_lookup,
    ))
    lines.append("")

    # Section 3 — daily winners.
    lines.append("## B. 每日漲幅榜首（看你錯過了誰）")
    lines.append("")
    lines.extend(_format_daily_gainers(daily_top_gainers, name_lookup))

    # Section 4 — reflection prompts.
    lines.append("## C. 反思（請誠實，不要 defensive）")
    lines.append("")
    lines.append(
        "1. **自評勝負**：你的推薦在 D1-D5 累計報酬如何？"
        "有沒有任何一檔在 D5 收盤負報酬？哪個訊號讓你誤判？"
    )
    lines.append(
        "2. **跨日贏家**：B 區塊裡是否有標的連續多日上榜？"
        "（多日連榜 = 高確信動能股，比一日噴出更值得追）"
        "你為什麼完全沒提到？回頭看 round 1 的 ctx 區塊"
        "（focus_briefs / news_sentiment / 外資台指期 / industry_rs / "
        "day_trading_trend / 借券 / overseas_indicators / upcoming_event 等），"
        "哪些徵兆其實已存在但被你忽略？請具體點名是哪一筆數據。"
    )
    lines.append(
        "3. **訊號權重檢討**：哪些訊號 round 1 給太高權重、結果是雜訊？"
        "哪些反過來太低權重、其實是真訊號？"
    )
    lines.append(
        "4. **缺失的資料**：要正確預測這些動能標的，"
        "目前 ctx 還缺哪一類資料？建議下次加入什麼新訊號？"
    )
    lines.append("")
    lines.append(
        "請聚焦在你**自己**的判斷檢討，不要泛泛而論大盤。"
        "回答完後本輪結束，使用者會再彙整一次最終結論。"
    )
    return "\n".join(lines)


# ── High-level entry point ───────────────────────────────────────


async def _resolve_thresholds(db: AsyncSession) -> tuple[float, int]:
    """Read the win threshold + window from runtime config, with the
    compiled defaults as fallback. Best-effort — any resolver hiccup
    just falls back to settings, never blocks the post-mortem."""
    threshold = settings.POST_MORTEM_WIN_THRESHOLD_PCT
    window = settings.POST_MORTEM_WINDOW_DAYS
    try:
        from services.runtime_config_service import (
            get_float as _get_float,
            get_int as _get_int,
        )
        threshold = await _get_float(db, "POST_MORTEM_WIN_THRESHOLD_PCT")
        window = await _get_int(db, "POST_MORTEM_WINDOW_DAYS")
    except Exception as exc:
        log.debug("post_mortem.runtime_config_fallback",
                  extra={"error": str(exc)})
    return threshold, window


async def build_post_mortem_message(
    db: AsyncSession,
    discussion: Discussion,
    *,
    n: int = _DEFAULT_TOP_N,
    days: int | None = None,
) -> PostMortemPayload:
    """Compute both ground-truth views + format the prompt.

    Returns a `PostMortemPayload`:
      - `trading_days == []` → archive doesn't reach 1 day past as_of.
        Caller surfaces a 400.
      - `verdict.status == "win"` → `prompt_text == ""` so the caller
        knows to skip the inject + new round entirely.
      - `verdict.status == "miss"` → `prompt_text` carries the
        漲幅榜複盤 prompt, ready for `inject_user_message`.
    """
    if discussion.as_of_date is None:
        raise ValueError(
            "post_mortem requires a backtest discussion (as_of_date is null)"
        )

    threshold_pct, window_days = await _resolve_thresholds(db)
    effective_days = days if days is not None else window_days
    # Resolve enough trading days to honour both the runtime window
    # AND any caller override; computed as max so bigger N still works.
    fetch_days = max(effective_days, _DEFAULT_DAYS)

    trading_days = await _resolve_trading_days_after(
        db, market=discussion.market,
        as_of=discussion.as_of_date, days=fetch_days,
    )
    if not trading_days:
        return PostMortemPayload(
            verdict=OutcomeVerdict(
                status="insufficient_data",
                threshold_pct=threshold_pct,
                window_days=window_days,
                winners=[],
                best_pct=None,
                reason="ohlcv_daily archive does not reach as_of+1",
            ),
        )

    recommended = []
    if discussion.conclusion:
        raw = discussion.conclusion.get("recommended_symbols") or []
        recommended = [str(s) for s in raw if s]

    rec_perf = await compute_recommended_performance(
        db, as_of=discussion.as_of_date, market=discussion.market,
        recommended_symbols=recommended, trading_days=trading_days,
    )
    daily = await compute_daily_top_gainers(
        db, market=discussion.market, trading_days=trading_days, n=n,
    )

    verdict = evaluate_recommendation_outcome(
        rec_perf,
        threshold_pct=threshold_pct,
        window_days=window_days,
        trading_days=trading_days,
    )

    # Best-effort: enrich symbol → 公司簡稱 from the in-memory map
    # populated by the daily `tw_symbol_map` cron. None when the
    # symbol isn't in the map; format_*_prompt handle that gracefully.
    name_lookup: dict[str, str | None] = {}
    if discussion.market == "TW":
        try:
            from services.tw_market_service import get_company_name
            symbols_seen = (
                {r.symbol for r in rec_perf}
                | {g.symbol for d in daily for g in d.gainers}
            )
            for sym in symbols_seen:
                name_lookup[sym] = get_company_name(sym)
        except Exception as exc:
            log.debug("post_mortem.name_lookup_failed",
                      extra={"error": str(exc)})

    if verdict.status == "win":
        # Skip-branch — leave prompt empty so the caller short-circuits
        # the inject + round chain. Callers still get the perf/daily
        # tables for UI surfacing of "why it was skipped".
        text = ""
    elif verdict.status == "miss":
        text = format_post_mortem_miss_prompt(
            as_of=discussion.as_of_date,
            trading_days=trading_days[:window_days],
            daily_top_gainers=daily[:window_days],
            recommended_symbols=recommended,
            verdict=verdict,
            company_name_lookup=name_lookup,
        )
    else:   # insufficient_data with trading_days present (rare:
            # window > 0 but no recommended symbols had bars)
        text = ""

    return PostMortemPayload(
        trading_days=trading_days,
        recommended_performance=rec_perf,
        daily_top_gainers=daily,
        prompt_text=text,
        verdict=verdict,
    )


# ── Serialisation helpers ────────────────────────────────────────


def gainer_row_to_dict(g: GainerRow) -> dict[str, Any]:
    """Serialise a single gainer for API responses + tests."""
    return {
        "symbol":       g.symbol,
        "change_pct":   g.change_pct,
        "close":        g.close,
        "base_close":   g.base_close,
        "trading_day":  g.trading_day.isoformat(),
    }


def recommended_performance_to_dict(
    r: RecommendedPerformance,
) -> dict[str, Any]:
    return {
        "symbol":     r.symbol,
        "base_close": r.base_close,
        "days": [
            {
                "trading_day": dp.trading_day.isoformat(),
                "close":       dp.close,
                "change_pct":  dp.change_pct,
            }
            for dp in r.days
        ],
    }


def daily_gainers_to_dict(d: DailyGainers) -> dict[str, Any]:
    return {
        "trading_day": d.trading_day.isoformat(),
        "gainers":     [gainer_row_to_dict(g) for g in d.gainers],
    }


def verdict_to_dict(v: OutcomeVerdict) -> dict[str, Any]:
    return {
        "status":         v.status,
        "threshold_pct":  v.threshold_pct,
        "window_days":    v.window_days,
        "winners": [
            {
                "symbol":   w.symbol,
                "peak_pct": w.peak_pct,
                "peak_day": w.peak_day.isoformat(),
            }
            for w in v.winners
        ],
        "best_pct":       v.best_pct,
        "reason":         v.reason,
    }
