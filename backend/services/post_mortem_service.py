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


OutcomeStatus = Literal[
    "miss", "marginal_win", "win", "strong_win", "insufficient_data",
]


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
    marginal_threshold_pct: float | None = None,
    strong_threshold_pct: float | None = None,
) -> OutcomeVerdict:
    """Classify the best recommended-symbol peak into a verdict band.

    Two modes:
      - **Legacy single-bar** (default): pass `threshold_pct` only.
        Returns `win` when any symbol's peak ≥ threshold, else `miss`.
        Used by older callers / tests.
      - **Three-tier** (post-mortem dispatcher): pass all three of
        `marginal_threshold_pct`, `threshold_pct` (= win bar),
        `strong_threshold_pct`. Classifies into:

          peak < marginal              → miss
          marginal ≤ peak < win        → marginal_win
          win ≤ peak < strong          → win
          peak ≥ strong                → strong_win

    `OutcomeVerdict.threshold_pct` is set to the bar that the result
    was classified against — i.e. for `marginal_win` it's the marginal
    bar (the one crossed), for `win` it's the win bar, for
    `strong_win` it's the strong bar, and for `miss` it's the lowest
    bar that was missed (= marginal in tiered mode, threshold_pct in
    legacy mode). This keeps the prompt's "通過/未達 X% 門檻" copy
    accurate per band.
    """
    tiered = (
        marginal_threshold_pct is not None
        and strong_threshold_pct is not None
    )
    miss_bar = (
        marginal_threshold_pct if tiered else threshold_pct
    )

    if not trading_days:
        return OutcomeVerdict(
            status="insufficient_data",
            threshold_pct=miss_bar,
            window_days=window_days,
            winners=[],
            best_pct=None,
            reason="no trading days resolved past as_of",
        )
    window = trading_days[:window_days]
    if not rec_perf:
        return OutcomeVerdict(
            status="insufficient_data",
            threshold_pct=miss_bar,
            window_days=window_days,
            winners=[],
            best_pct=None,
            reason="no recommended symbols had entry-day bars",
        )

    best_pct: float | None = None
    qualified: list[WinnerEntry] = []
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
        if peak_pct >= miss_bar:
            qualified.append(WinnerEntry(
                symbol=r.symbol,
                peak_pct=round(peak_pct, 4),
                peak_day=peak_day,
            ))

    qualified_sorted = sorted(
        qualified, key=lambda w: w.peak_pct, reverse=True,
    )

    if not tiered:
        # Legacy binary win/miss against the single bar.
        if qualified:
            return OutcomeVerdict(
                status="win",
                threshold_pct=threshold_pct,
                window_days=window_days,
                winners=qualified_sorted,
                best_pct=(
                    round(best_pct, 4) if best_pct is not None else None
                ),
                reason=(
                    f"{len(qualified)} symbol(s) reached >= "
                    f"{threshold_pct:g}% within {window_days} trading days"
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

    # Tiered classification. `qualified` already contains every symbol
    # whose peak ≥ marginal — the band classification just looks at
    # the BEST peak to decide which prompt to fire.
    if best_pct is None or best_pct < marginal_threshold_pct:
        return OutcomeVerdict(
            status="miss",
            threshold_pct=marginal_threshold_pct,
            window_days=window_days,
            winners=[],
            best_pct=round(best_pct, 4) if best_pct is not None else None,
            reason=(
                f"all recommendations peaked below "
                f"{marginal_threshold_pct:g}% within {window_days} "
                f"trading days"
            ),
        )

    if best_pct >= strong_threshold_pct:
        band_status: OutcomeStatus = "strong_win"
        band_threshold = strong_threshold_pct
    elif best_pct >= threshold_pct:
        band_status = "win"
        band_threshold = threshold_pct
    else:
        band_status = "marginal_win"
        band_threshold = marginal_threshold_pct

    return OutcomeVerdict(
        status=band_status,
        threshold_pct=band_threshold,
        window_days=window_days,
        winners=qualified_sorted,
        best_pct=round(best_pct, 4),
        reason=(
            f"{len(qualified_sorted)} symbol(s) reached >= "
            f"{band_threshold:g}% (best {best_pct:.2f}%) within "
            f"{window_days} trading days"
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


def format_post_mortem_win_prompt(
    *,
    as_of: date,
    trading_days: list[date],
    recommended_performance: list[RecommendedPerformance],
    daily_top_gainers: list[DailyGainers],
    recommended_symbols: list[str],
    verdict: OutcomeVerdict,
    company_name_lookup: dict[str, str | None] | None = None,
) -> str:
    """PR-B3 — win-side post-mortem prompt.

    Asks the personas to identify *what specifically worked* so the
    learning loop captures the signal combinations and thesis
    elements that pay off, rather than letting the success roll by
    untaught. The hard guard against survivor bias is question 2:
    "list at least one signal that was uncertain at the time but
    turned out to be key." Without that explicit ask the LLM tends
    to spin a confident retrospective narrative that overfits the
    one win.

    The win-prompt feeds the synthesizer's `lessons` extraction; the
    synthesizer is expected to use the new categories
    `correct_signal_combo` (specific signal interactions) and
    `successful_thesis` (the broader thesis that played out) on
    win-case post-mortems.

    Always renders the per-day top-N leaderboard alongside the
    self-eval table — even on a clear win, surfacing 30%-gainers
    the personas missed prevents "I got 5%, perfect" complacency.
    """
    name_lookup = company_name_lookup or {}
    lines: list[str] = ["【事後檢討 — 命中經驗】", ""]

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
        f"✅ 你的推薦 {', '.join(recommended_symbols) or '（無）'} 在 "
        f"{verdict.window_days} 個交易日內最高達 {best}，"
        f"通過 {verdict.threshold_pct:g}% 門檻。"
    )
    lines.append("")

    lines.append("## A. 你的推薦 D1-D5 表現")
    lines.append("")
    lines.extend(_format_recommended_table(
        recommended_performance, trading_days, name_lookup,
    ))
    lines.append("")

    lines.append("## B. 每日漲幅榜首（看你錯過了誰）")
    lines.append("")
    lines.extend(_format_daily_gainers(daily_top_gainers, name_lookup))

    lines.append("## C. 反思（要誠實面對 survivor bias，不要事後諸葛）")
    lines.append("")
    lines.append(
        "1. **真正的關鍵訊號**：哪一筆 ctx 資料"
        "（focus_briefs / news_sentiment / 外資台指期 / industry_rs / "
        "single_stock_futures_oi / day_trading_trend / 借券 / "
        "overseas_indicators / upcoming_event 等）是這次命中的**充分必要**訊號？"
        "請只點名 1-3 筆，不要把所有訊號都歸功一遍。"
    )
    lines.append(
        "2. **當時的不確定點**：圈出 round 1 中你**沒把握、評估為弱訊號**的 "
        "≥1 條判讀，事後證明卻是關鍵。如果你想不到，這次命中很可能是運氣，"
        "請明說「沒有特別不確定的訊號」而不要硬找一個。"
    )
    lines.append(
        "3. **可重現的訊號組合**：把命中的關鍵抽象化成 `correct_signal_combo`"
        "（例：「外資台指期由空轉多 + 單日五日均量爆量 + 殖利率倒掛緩解」）。"
        "這個組合在歷史上出現幾次？你預期下次再出現時的勝率？"
    )
    lines.append(
        "4. **可推廣的論點**：把這次命中的 thesis 抽象化成 `successful_thesis`"
        "（例：「外資轉多 + 風險偏好回升 → 半導體先行」）。"
        "下一次同 thesis 但不同 sector / region 還能成立嗎？"
    )
    lines.append(
        "5. **B 區塊的更大贏家**：每日漲幅榜上有沒有任何標的漲幅遠超你的最佳推薦"
        "（>2× 你的 peak）？如果有，這代表 round 1 缺了什麼類別的訊號？"
        "把它列為 `missing_signal_category` lesson。"
    )
    lines.append("")
    lines.append(
        "請務必使用 `correct_signal_combo` 跟 `successful_thesis` 作為 "
        "lessons 的 category，這樣下次討論的 ctx 才能正確分類為命中經驗。"
    )
    lines.append(
        "請聚焦在「為什麼這次對了，且什麼時候會再對一次」，不要泛泛吹捧自己。"
        "回答完後本輪結束，使用者會再彙整一次最終結論。"
    )
    return "\n".join(lines)


def format_post_mortem_marginal_win_prompt(
    *,
    as_of: date,
    trading_days: list[date],
    recommended_performance: list[RecommendedPerformance],
    daily_top_gainers: list[DailyGainers],
    recommended_symbols: list[str],
    verdict: OutcomeVerdict,
    company_name_lookup: dict[str, str | None] | None = None,
) -> str:
    """Marginal-win post-mortem: peak crossed the lower band but not
    the win bar. Skeptical framing — TW large-caps regularly clear
    3% in a 5-session window by noise alone, so a marginal-win is
    closer to "didn't lose" than to a real success. The prompt
    forces the personas to defend whether the call was skill or
    coin-flip, and explicitly compares against the daily leaderboard
    to make sure they didn't merely pick a stock that drifted up
    while the actual movers ran ahead."""
    name_lookup = company_name_lookup or {}
    lines: list[str] = ["【事後檢討 — 勉強過線】", ""]

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
        f"⚠️ 你的推薦 {', '.join(recommended_symbols) or '（無）'} 在 "
        f"{verdict.window_days} 個交易日內最高僅 {best}，"
        f"勉強過 {verdict.threshold_pct:g}% 但沒過 win 門檻。"
        f"在 TW 大型股 5 日窗口裡這個幅度離雜訊很近，**不要當成命中**。"
    )
    lines.append("")

    lines.append("## A. 你的推薦 D1-D5 表現")
    lines.append("")
    lines.extend(_format_recommended_table(
        recommended_performance, trading_days, name_lookup,
    ))
    lines.append("")

    lines.append("## B. 每日漲幅榜首（看真正的贏家在哪裡）")
    lines.append("")
    lines.extend(_format_daily_gainers(daily_top_gainers, name_lookup))

    lines.append("## C. 反思（請直球面對「這是技術還是運氣」）")
    lines.append("")
    lines.append(
        "1. **訊號是否真的存在**：這次勉強過線，請誠實判斷 round 1 的"
        "關鍵訊號是「明確指向上漲」還是「方向模糊但你選了多」？"
        "如果是後者，請直接標記 `coin_flip` lesson。"
    )
    lines.append(
        "2. **真正的贏家差距**：B 區塊裡有沒有任何標的漲幅 ≥3× 你的最佳推薦？"
        "如果有，請點名是哪一檔、是什麼產業，並回頭看 round 1 的 ctx —— "
        "（focus_briefs / news_sentiment / 外資台指期 / industry_rs / "
        "single_stock_futures_oi / day_trading_trend / 借券 / "
        "overseas_indicators / upcoming_event 等）—— 是否其實有訊號指向那一檔？"
    )
    lines.append(
        "3. **這次該不該寫進 lessons**：如果 (1) 是 coin_flip 而 (2) 有更明顯的"
        "錯過，請以 miss 角度寫 lesson（`missed_signal` / `missing_signal_category`），"
        "**不要**用 `correct_signal_combo`。"
        "勉強過線寫成命中會污染下次的命中經驗 ctx。"
    )
    lines.append(
        "4. **下次的訊號補強**：要把這次的 marginal call 升級成 win 等級，"
        "round 1 需要新增哪一類訊號？請具體列出資料來源。"
    )
    lines.append("")
    lines.append(
        "請務必區分 `coin_flip` / `missed_signal` / `missing_signal_category` "
        "這幾個 category，不要因為 peak >3% 就硬寫成 `correct_signal_combo`。"
        "回答完後本輪結束，使用者會再彙整一次最終結論。"
    )
    return "\n".join(lines)


def format_post_mortem_strong_win_prompt(
    *,
    as_of: date,
    trading_days: list[date],
    recommended_performance: list[RecommendedPerformance],
    daily_top_gainers: list[DailyGainers],
    recommended_symbols: list[str],
    verdict: OutcomeVerdict,
    company_name_lookup: dict[str, str | None] | None = None,
) -> str:
    """Strong-win post-mortem: peak ≥10%. In a 5-session TW window
    that magnitude is almost always sector-wide or macro driven
    (大盤齊揚 / 類股輪動 / 重大事件). The prompt forces the
    personas to disambiguate stock-picking from regime-riding
    BEFORE they extract a `correct_signal_combo` lesson, otherwise
    the learning loop bakes in "I picked 2330 in March 2026" =
    "I'm a great stock picker" when really the whole semiconductor
    sector ran 15%."""
    name_lookup = company_name_lookup or {}
    lines: list[str] = ["【事後檢討 — 強勢命中】", ""]

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
        f"🚀 你的推薦 {', '.join(recommended_symbols) or '（無）'} 在 "
        f"{verdict.window_days} 個交易日內最高達 {best}，"
        f"通過 {verdict.threshold_pct:g}% 強勢門檻。"
        f"5 個交易日 ≥10% 的幅度在 TW 通常**不是純粹個股因素**，"
        f"請先區分這是 stock-picking 還是 regime-riding。"
    )
    lines.append("")

    lines.append("## A. 你的推薦 D1-D5 表現")
    lines.append("")
    lines.extend(_format_recommended_table(
        recommended_performance, trading_days, name_lookup,
    ))
    lines.append("")

    lines.append("## B. 每日漲幅榜首（檢查是不是同類股齊漲）")
    lines.append("")
    lines.extend(_format_daily_gainers(daily_top_gainers, name_lookup))

    lines.append("## C. 反思（先洗 regime，再談 picking）")
    lines.append("")
    lines.append(
        "1. **regime 還是 picking**：這 5 個交易日 TAIEX / 你推薦標的的"
        "**同產業**漲幅是多少？如果同類股普遍 +8% 以上，那這次 +10% 主要是"
        "regime-riding，請標記 `regime_context` lesson"
        "（例：「2026-Q1 半導體類股齊漲 +12%，個股 alpha 不顯著」）。"
        "**不要**直接寫 `correct_signal_combo`。"
    )
    lines.append(
        "2. **個股 alpha 在哪裡**：扣掉同類股漲幅後，你的推薦還有 alpha 嗎？"
        "如果 alpha < 2%，這次的「強勢命中」其實只是搭上 regime，"
        "請誠實寫成 `regime_capture` 而非 stock-picking 的功勞。"
    )
    lines.append(
        "3. **可重現的訊號組合**：在 alpha 確實存在（>2%）的前提下，"
        "把命中的關鍵抽象化成 `correct_signal_combo`"
        "（例：「外資台指期由空轉多 + 單日五日均量爆量 + 殖利率倒掛緩解」）。"
        "如果 alpha 不存在，跳過這題不要硬找。"
    )
    lines.append(
        "4. **regime 識別清單**：回頭看 round 1，當時哪些 ctx 訊號"
        "（外資台指期 / industry_rs / overseas_indicators / upcoming_event 等）"
        "其實已經在說「regime 風向已轉」？把這些抽象化成 `regime_signal` lesson "
        "供下次討論辨識同類 regime 時使用。"
    )
    lines.append(
        "5. **B 區塊的同類股 / 跨類股比例**：B 區塊裡上榜標的是集中在"
        "你推薦的產業，還是分散到多個產業？前者 = regime-riding 證據；"
        "後者 = 大盤齊揚，更不能歸功於你的 stock-picking。"
    )
    lines.append("")
    lines.append(
        "請優先使用 `regime_context` / `regime_capture` / `regime_signal` 三個 category，"
        "只在確實存在 alpha 時才使用 `correct_signal_combo`。"
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


async def _resolve_thresholds(
    db: AsyncSession,
) -> tuple[float, float, float, int]:
    """Read the four post-mortem tunables from runtime config, with the
    compiled defaults as fallback. Returns
    `(marginal, win, strong, window_days)`.

    Best-effort — any resolver hiccup falls back to compiled settings
    rather than blocking the post-mortem flow. If the on-DB values
    drift out of order we ignore them and fall back to compiled
    defaults so the classifier never receives a degenerate band
    layout. The runtime upsert path enforces ordering at write time
    so this is purely defensive."""
    marginal = settings.POST_MORTEM_MARGINAL_WIN_THRESHOLD_PCT
    win_bar = settings.POST_MORTEM_WIN_THRESHOLD_PCT
    strong = settings.POST_MORTEM_STRONG_WIN_THRESHOLD_PCT
    window = settings.POST_MORTEM_WINDOW_DAYS
    try:
        from services.runtime_config_service import (
            get_float as _get_float,
            get_int as _get_int,
        )
        marginal = await _get_float(
            db, "POST_MORTEM_MARGINAL_WIN_THRESHOLD_PCT",
        )
        win_bar = await _get_float(db, "POST_MORTEM_WIN_THRESHOLD_PCT")
        strong = await _get_float(
            db, "POST_MORTEM_STRONG_WIN_THRESHOLD_PCT",
        )
        window = await _get_int(db, "POST_MORTEM_WINDOW_DAYS")
    except Exception as exc:
        log.debug("post_mortem.runtime_config_fallback",
                  extra={"error": str(exc)})

    if not (marginal < win_bar < strong):
        log.warning(
            "post_mortem.threshold_ordering_invalid_fallback",
            extra={"marginal": marginal, "win": win_bar, "strong": strong},
        )
        marginal = settings.POST_MORTEM_MARGINAL_WIN_THRESHOLD_PCT
        win_bar = settings.POST_MORTEM_WIN_THRESHOLD_PCT
        strong = settings.POST_MORTEM_STRONG_WIN_THRESHOLD_PCT
    return marginal, win_bar, strong, window


async def build_post_mortem_message(
    db: AsyncSession,
    discussion: Discussion,
    *,
    n: int = _DEFAULT_TOP_N,
    days: int | None = None,
) -> PostMortemPayload:
    """Compute both ground-truth views + format the band-specific prompt.

    Returns a `PostMortemPayload`:
      - `trading_days == []` → archive doesn't reach 1 day past as_of.
        Caller surfaces a 400.
      - `verdict.status` ∈ {`miss`, `marginal_win`, `win`, `strong_win`}
        → `prompt_text` carries the band-specific 事後檢討 prompt
        (every band now includes the per-day top-N leaderboard).
      - `verdict.status == "insufficient_data"` (rare) →
        `prompt_text == ""`.
    """
    if discussion.as_of_date is None:
        raise ValueError(
            "post_mortem requires a backtest discussion (as_of_date is null)"
        )

    marginal_pct, win_pct, strong_pct, window_days = (
        await _resolve_thresholds(db)
    )
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
                threshold_pct=marginal_pct,
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
        threshold_pct=win_pct,
        marginal_threshold_pct=marginal_pct,
        strong_threshold_pct=strong_pct,
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

    window_days_truncated = trading_days[:window_days]
    daily_truncated = daily[:window_days]

    if verdict.status == "strong_win":
        text = format_post_mortem_strong_win_prompt(
            as_of=discussion.as_of_date,
            trading_days=window_days_truncated,
            recommended_performance=rec_perf,
            daily_top_gainers=daily_truncated,
            recommended_symbols=recommended,
            verdict=verdict,
            company_name_lookup=name_lookup,
        )
    elif verdict.status == "win":
        text = format_post_mortem_win_prompt(
            as_of=discussion.as_of_date,
            trading_days=window_days_truncated,
            recommended_performance=rec_perf,
            daily_top_gainers=daily_truncated,
            recommended_symbols=recommended,
            verdict=verdict,
            company_name_lookup=name_lookup,
        )
    elif verdict.status == "marginal_win":
        text = format_post_mortem_marginal_win_prompt(
            as_of=discussion.as_of_date,
            trading_days=window_days_truncated,
            recommended_performance=rec_perf,
            daily_top_gainers=daily_truncated,
            recommended_symbols=recommended,
            verdict=verdict,
            company_name_lookup=name_lookup,
        )
    elif verdict.status == "miss":
        text = format_post_mortem_miss_prompt(
            as_of=discussion.as_of_date,
            trading_days=window_days_truncated,
            daily_top_gainers=daily_truncated,
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
