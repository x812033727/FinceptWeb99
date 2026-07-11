"""B1 個股 AI 研究報告 — context assembly + prompt + persistence.

The context is NOT reassembled by hand: every block is one of the
discussion subsystem's existing builders
(`services/discussion/context/blocks/*`), pointed at a single focus
symbol. That keeps the report's data identical to what the discussion
personas see (same waterfalls, same caches, same backtest-safe
readers) and means upgrades to those blocks flow into reports for
free:

  * `http.fetch_focus_briefs`        → 報價 / 技術面(均線、RSI、52 週)/
                                       估值(本益比、淨值比、殖利率、EPS)/
                                       月營收趨勢 / 籌碼 5 日(三大法人、
                                       融資融券)/ 同業比較 (TW),
                                       quote+technicals+fundamentals (US)
  * `technical.fetch_short_term_signals` → 短線動能(5 日/20 日報酬、
                                       量能比、RSI14、跳空)
  * `news.fetch_per_symbol_sentiment`    → 個股新聞情緒(7 日)
  * `announcements.fetch_corporate_announcements` → 重大訊息(per-symbol)
  * `chip.fetch_broker_concentration`    → 主力分點買賣超 (TW only)

Errors degrade per-block exactly like a discussion round: a failing
connector blanks one key and lands in ``ctx["errors"]`` so the prompt
can say "資料不足" instead of the whole report failing.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from models.stock_report import StockReport

log = logging.getLogger(__name__)

# Section headings the model must emit (## 摘要 … ## 結論). Shared by
# the prompt contract below and the `parse_report_sections` splitter.
REPORT_SECTIONS = ["摘要", "基本面", "籌碼與資金", "技術面", "風險", "結論"]

_SECTION_HEADING_RE = re.compile(r"^##\s*(.+?)\s*$", re.MULTILINE)

SYSTEM_PROMPT = (
    "你是一位資深證券研究分析師,擅長台股與美股的個股深度研究。"
    "請根據 <market_context> 內即時整理的結構化資料,撰寫一份"
    "繁體中文(台灣用語)的個股研究報告。\n\n"
    "輸出格式(Markdown,六個二級標題必須完全一致、依序出現):\n"
    "## 摘要\n## 基本面\n## 籌碼與資金\n## 技術面\n## 風險\n## 結論\n\n"
    "撰寫要求:\n"
    "- 只引用 <market_context> 中確實存在的數據;缺漏的欄位請明確寫"
    "「資料不足」,絕對不可虛構數字。\n"
    "- 「基本面」涵蓋估值(本益比、淨值比、殖利率、EPS)與營收趨勢;"
    "台股請引用月營收年增率(YoY)。\n"
    "- 「籌碼與資金」台股請引用三大法人買賣超、融資融券與主力分點;"
    "美股籌碼資料有限時請說明並改以量能與資金面觀察代替。\n"
    "- 「技術面」引用均線(20/60 日)、RSI、52 週區間位置與短線動能"
    "(5 日/20 日報酬、量能比)。\n"
    "- 「風險」至少列出 3 點具體、可驗證的風險因素。\n"
    "- 「結論」給出偏多/中性/偏空的整體研究觀點與後續觀察條件,"
    "並在最後一行以粗體標示:"
    "「**本報告由 AI 產生,僅供研究參考,非投資建議。**」\n"
    "- 全文約 800~1200 字,金融術語使用台灣慣用語"
    "(殖利率、本益比、三大法人等)。"
)


def _make_error_recorder(ctx: dict[str, Any]):
    """Same uniform diagnostic surface the discussion builder uses —
    log at WARNING (a single-block outage is expected operational
    noise here) + append `{source, error}` to ``ctx['errors']``."""
    def _record(source: str, exc: Exception) -> None:
        log.warning(
            "stock_report.context.connector_failed",
            extra={"source": source, "error": str(exc)},
        )
        ctx["errors"].append({"source": source, "error": str(exc)})
    return _record


async def assemble_report_context(
    db: AsyncSession,
    *,
    market: str,
    symbol: str,
) -> dict[str, Any]:
    """Gather the per-symbol market context by reusing the discussion
    context block builders (live mode, single focus symbol).

    The ctx dict mirrors the discussion builder's key contract for
    the blocks we fire, so each block writes into the key it already
    knows. All keys are pre-seeded to their empty shape — a failed
    block leaves "no signal", never a missing key.
    """
    from services.discussion.context.blocks import (
        announcements,
        chip,
        http,
        news,
        technical,
    )

    focus = [symbol]
    ctx: dict[str, Any] = {
        "market": market,
        "focus_symbols": focus,
        "focus_briefs": [],
        "short_term_signals": {},
        "per_symbol_news_sentiment": {},
        "corporate_announcements": {"market": [], "per_symbol": {}},
        "broker_concentration": [],
        "errors": [],
    }
    record_error = _make_error_recorder(ctx)

    # HTTP-bound blocks (autosession service helpers — don't touch the
    # shared `db`).
    await http.fetch_focus_briefs(
        ctx, market=market, focus_symbols=focus,
        as_of=None, record_error=record_error,
    )
    if market == "TW":
        # 主力分點 — live FinMind read behind a 24h Redis cache.
        await chip.fetch_broker_concentration(
            ctx, focus_symbols=focus, as_of=None, record_error=record_error,
        )

    # DB-bound blocks — sequential on the shared session (SQLAlchemy
    # AsyncSession is not safe across concurrent awaits).
    await technical.fetch_short_term_signals(
        ctx, db, market=market, focus_symbols=focus,
        as_of=None, record_error=record_error, max_focus_symbols=1,
    )
    await news.fetch_per_symbol_sentiment(
        ctx, db, market=market, focus_symbols=focus,
        as_of_dt=None, record_error=record_error, max_focus_symbols=1,
    )
    await announcements.fetch_corporate_announcements(
        ctx, db, market=market, focus_symbols=focus,
        as_of_dt=None, record_error=record_error,
    )
    # The announcements block also returns the market-wide feed (it's
    # built for whole-market discussions). A single-stock report only
    # needs this symbol's disclosures — drop the market list so the
    # prompt token budget isn't spent on other issuers' announcements.
    if isinstance(ctx.get("corporate_announcements"), dict):
        ctx["corporate_announcements"]["market"] = []

    return ctx


def build_report_messages(
    ctx: dict[str, Any], *, market: str, symbol: str,
) -> list[dict[str, str]]:
    """System + user message pair for the single-call report LLM."""
    briefs = ctx.get("focus_briefs") or []
    brief = briefs[0] if briefs and isinstance(briefs[0], dict) else {}
    name = brief.get("name_zh") or brief.get("name") or ""
    label = f"{symbol}({name})" if name else symbol
    ctx_json = json.dumps(ctx, ensure_ascii=False, default=str)
    user = (
        f"請為 {market} 市場的個股 {label} 撰寫研究報告。\n\n"
        f"<market_context>\n{ctx_json}\n</market_context>"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def parse_report_sections(content: str) -> dict[str, str] | None:
    """Best-effort split of the markdown report into `{標題: 內文}`.

    Returns None when no `## ` headings are present (model ignored
    the format contract) — the caller stores NULL and `content_md`
    remains the source of truth.
    """
    matches = list(_SECTION_HEADING_RE.finditer(content))
    if not matches:
        return None
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        out[m.group(1)] = content[start:end].strip()
    return out or None


async def persist_report(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    symbol: str,
    market: str,
    content_md: str,
    model: str,
) -> StockReport:
    """Insert one completed report row (called after the SSE stream
    finished with non-empty content)."""
    report = StockReport(
        user_id=user_id,
        symbol=symbol,
        market=market,
        content_md=content_md,
        model=model,
        sections=parse_report_sections(content_md),
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)
    return report
