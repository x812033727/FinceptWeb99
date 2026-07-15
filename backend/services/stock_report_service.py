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
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from models.stock_report import StockReport

log = logging.getLogger(__name__)

# Section headings the model must emit (## 摘要 … ## 結論). Shared by
# the prompt contract below and the `parse_report_sections` splitter.
REPORT_SECTIONS = ["摘要", "基本面", "籌碼與資金", "技術面", "風險", "結論"]
PROMPT_VERSION = "stock-report-v3-source-quality"

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
    "- 每一個數字與具體市場事實後必須緊接其證據編號,格式為 [E1]。"
    "只能使用 <evidence> 中存在的編號;沒有證據時必須寫「資料不足」。\n"
    "- 若 <quality_summary> 顯示資料源衝突、過期或未完成交叉驗證,"
    "必須在「摘要」與「風險」揭露;被移除為 null 的衝突數值不得推測或引用。\n"
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
    # A report is a high-trust surface, so pay for one explicit independent
    # quote check even though ordinary market lists stay on the low-cost
    # waterfall path. Failure degrades to an unverified diagnostic rather than
    # aborting the remaining context blocks.
    briefs = ctx.get("focus_briefs") or []
    if briefs and isinstance(briefs[0], dict) and isinstance(briefs[0].get("quote"), dict):
        quote = briefs[0]["quote"]
        try:
            if market == "TW":
                from services import tw_market_service as market_service
            else:
                from services import us_market_service as market_service
            quote["quality_check"] = await market_service.verify_quote_consistency(symbol, quote)
        except Exception as exc:
            record_error("quote_consistency", exc)
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


def prepare_traceable_context(ctx: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], datetime]:
    """Fail closed on degraded blocks and create a numeric evidence snapshot.

    Every numeric leaf that remains available receives a stable evidence id.
    The sanitized context and evidence list are stored with the report, making
    the prompt reproducible without calling an upstream provider again.
    """
    evidence: list[dict[str, Any]] = []
    quality_issues: list[dict[str, str]] = []

    def quality_diagnostic(value: Any) -> Any:
        """Retain provenance without leaking disputed numeric observations."""
        if not isinstance(value, dict):
            return value
        return {
            key: child for key, child in value.items()
            if key in {
                "status", "consistency", "primary_source", "secondary_source",
                "cross_checked_sources", "flags", "quality_flags", "checked_at",
            }
        }

    def walk(
        value: Any,
        path: str,
        source: str = "context",
        as_of: str | None = None,
        *,
        collect_evidence: bool = True,
    ) -> Any:
        if isinstance(value, dict):
            local_source = str(value.get("data_source") or value.get("source") or source)
            local_as_of = value.get("as_of_session") or value.get("as_of") \
                or value.get("fetched_at") or value.get("date") or as_of
            quality_check = value.get("quality_check") if isinstance(value.get("quality_check"), dict) else {}
            meta = value.get("meta") if isinstance(value.get("meta"), dict) else {}
            consistency = quality_check.get("status") or meta.get("consistency")
            degraded_status = (
                "conflict" if consistency == "conflict" else
                "unavailable" if local_source == "unavailable" else
                "stale" if (
                    "stale" in local_source
                    or value.get("is_stale") is True
                    or meta.get("freshness") in {"stale", "unavailable"}
                ) else
                "unverified" if consistency == "unverified" else None
            )
            degraded = degraded_status in {"conflict", "stale", "unavailable"}
            if degraded_status:
                quality_issues.append({
                    "path": path or "context",
                    "status": degraded_status,
                    "source": local_source,
                })
            degraded = degraded or (
                local_source == "unavailable"
                or "stale" in local_source
                or value.get("is_stale") is True
                or meta.get("freshness") in {"stale", "unavailable"}
            )
            out: dict[str, Any] = {}
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else key
                if key in {"quality_check", "meta"}:
                    out[key] = quality_diagnostic(child)
                elif degraded and isinstance(child, int | float) and not isinstance(child, bool):
                    out[key] = None
                else:
                    out[key] = walk(
                        child, child_path, local_source,
                        str(local_as_of) if local_as_of else None,
                        collect_evidence=collect_evidence,
                    )
            if degraded:
                out["quality_status"] = degraded_status or "stale"
            elif degraded_status == "unverified":
                out["quality_status"] = "unverified"
            return out
        if isinstance(value, list):
            return [
                walk(child, f"{path}[{i}]", source, as_of, collect_evidence=collect_evidence)
                for i, child in enumerate(value)
            ]
        if collect_evidence and isinstance(value, int | float) and not isinstance(value, bool):
            evidence.append({
                "id": f"E{len(evidence) + 1}",
                "type": "numeric",
                "path": path,
                "value": value,
                "source": source,
                "as_of": as_of,
            })
        elif collect_evidence and isinstance(value, str) and path.rsplit(".", 1)[-1] in {
            "title", "body", "summary", "category", "label",
        } and value.strip():
            evidence.append({
                "id": f"E{len(evidence) + 1}",
                "type": "fact",
                "path": path,
                "value": value,
                "source": source,
                "as_of": as_of,
            })
        return value

    sanitized = walk(ctx, "")
    error_count = len(ctx.get("errors") or []) if isinstance(ctx.get("errors"), list) else 0
    counts = {
        status: sum(issue["status"] == status for issue in quality_issues)
        for status in ("conflict", "stale", "unavailable", "unverified")
    }
    penalty = min(
        0.75,
        counts["conflict"] * 0.25
        + counts["stale"] * 0.15
        + counts["unavailable"] * 0.15
        + counts["unverified"] * 0.05
        + error_count * 0.05,
    )
    reliability = round(1.0 - penalty, 4)
    band = "high" if reliability >= 0.9 else "moderate" if reliability >= 0.7 else "low"
    sanitized["quality_summary"] = {
        "reliability_score": reliability,
        "band": band,
        "issue_counts": counts,
        "connector_errors": error_count,
        "issues": quality_issues,
    }
    cutoff = datetime.now(UTC)
    sanitized["data_cutoff"] = cutoff.isoformat()
    sanitized["evidence"] = evidence
    return sanitized, evidence, cutoff


def report_reliability(ctx: dict[str, Any]) -> float:
    summary = ctx.get("quality_summary") if isinstance(ctx, dict) else None
    if not isinstance(summary, dict):
        return 1.0
    try:
        return max(0.0, min(1.0, float(summary.get("reliability_score", 1.0))))
    except (TypeError, ValueError):
        return 1.0


def score_report_quality(content: str, evidence: list[dict[str, Any]]) -> float:
    """Score line-level numerical consistency against cited evidence."""
    values = {item["id"]: item.get("value") for item in evidence}
    total = covered = 0
    for line in content.splitlines():
        tokens = re.findall(r"(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)?%?", line)
        if not tokens:
            continue
        citations = re.findall(r"\[(E\d+)\]", line)
        cited_values = {str(values[c]).replace(",", "") for c in citations if c in values}
        for token in tokens:
            total += 1
            normalized = token.lstrip("+").rstrip("%").replace(",", "")
            try:
                number = float(normalized)
            except ValueError:
                continue
            if any(abs(number - float(value)) < 1e-9 for value in cited_values):
                covered += 1
    if not total:
        return 1.0
    return round(covered / total, 4)


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
    prompt_version: str = PROMPT_VERSION,
    data_cutoff: datetime | None = None,
    evidence: list[dict[str, Any]] | None = None,
    context_snapshot: dict[str, Any] | None = None,
    quality_score: float | None = None,
) -> StockReport:
    """Insert one completed report row (called after the SSE stream
    finished with non-empty content)."""
    report = StockReport(
        user_id=user_id,
        symbol=symbol,
        market=market,
        content_md=content_md,
        model=model,
        model_id=model,
        prompt_version=prompt_version,
        data_cutoff=data_cutoff,
        evidence=evidence or [],
        context_snapshot=context_snapshot,
        quality_score=quality_score if quality_score is not None else score_report_quality(content_md, evidence or []),
        sections=parse_report_sections(content_md),
    )
    db.add(report)
    await db.commit()
    # UUID and timestamps use Python-side defaults and are populated by the
    # commit flush. A post-commit refresh adds no data, while SSE dependency
    # teardown can detach the instance between awaits under load.
    return report
