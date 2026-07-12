"""B5 AI 投組健檢 — context assembly + prompt (no persistence).

The review is an on-demand analysis over data the platform already
computes; NOTHING is re-implemented here:

  * `services.portfolio_risk_service.get_portfolio_risk` — the exact
    function behind ``GET /api/portfolio/{id}/risk`` (feature C1).
    One call yields ownership enforcement (raises ``ValueError`` for
    missing/foreign portfolios), live holding weights, three-method
    VaR, vol/Sharpe/Sortino/maxDD/beta, the correlation matrix and
    concentration warnings.
  * `services.regime_classifier.classify_regimes` — the timeline
    overlay's regime engine. We ask it for a recent window and read
    off the bands active on the latest data day to label the current
    market regime (bull/bear × high_vol/low_vol; TW-only for now,
    matching the classifier's coverage).

user-scoped 防越權: context assembly is a direct service call that
receives the *authenticated* ``user_id`` — there is no LLM-directed
tool that could be pointed at another user's portfolio, and a foreign
``portfolio_id`` dies with ``ValueError`` before any prompt is built.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# Section headings the model must emit, in order. The frontend renders
# free markdown so this is a prompt contract only (no parser needed —
# reviews are not persisted).
REVIEW_SECTIONS = ["總評", "集中度與風險", "與當前市場情勢的適配", "行動建議"]

# How far back to ask the regime classifier. 200d MA warmup is handled
# inside `classify_regimes`; this window only bounds the emitted bands,
# and 90 days comfortably covers "what regime are we in right now".
_REGIME_LOOKBACK_DAYS = 90

# zh-TW labels so the prompt doesn't force the model to translate the
# classifier's internal tags (and can't mistranslate the thresholds).
_REGIME_LABELS_ZH = {
    "bull": "多頭(指數站上 200 日均線且 RSI > 50)",
    "bear": "空頭(指數跌破 200 日均線)",
    "high_vol": "高波動(VIX > 25)",
    "low_vol": "低波動(VIX < 15)",
}

SYSTEM_PROMPT = (
    "你是一位資深投資組合顧問,擅長資產配置、風險管理與市場情勢判讀。"
    "請根據 <portfolio_context> 內即時計算的投組風險數據與市場情勢資料,"
    "以繁體中文(台灣用語)為使用者的投資組合撰寫一份健檢報告。\n\n"
    "輸出格式(Markdown,四個二級標題必須完全一致、依序出現):\n"
    "## 總評\n## 集中度與風險\n## 與當前市場情勢的適配\n## 行動建議\n\n"
    "撰寫要求:\n"
    "- 只引用 <portfolio_context> 中確實存在的數據(持倉權重、VaR、"
    "年化波動度、Sharpe、Sortino、最大回撤、beta、相關性、集中度警示、"
    "市場情勢標籤);缺漏的欄位請明確寫「資料不足」,絕對不可虛構數字。\n"
    "- 「總評」以 3~5 句話概括投組的整體體質(規模、配置輪廓、風險水準)。\n"
    "- 「集中度與風險」引用單一持倉權重與市場桶集中度警示(warnings)、"
    "三種方法的 95% VaR、年化波動度、最大回撤與持倉間相關性;"
    "被排除於風險計算的持倉(excluded)請說明其影響。\n"
    "- 「與當前市場情勢的適配」根據 market_regime 的情勢標籤"
    "(多頭/空頭/高波動/低波動)評估目前配置是否合適;"
    "情勢資料僅涵蓋台股大盤時,請說明對非台股部位的適用限制;"
    "情勢資料不足時請直接寫「資料不足」。\n"
    "- 「行動建議」列出 2~4 條具體、可執行的調整建議,每一條都必須"
    "附上依據數據的理由(格式:「建議 — 理由」),並標註優先順序;"
    "只能建議調整方向,不可指示實際下單。\n"
    "- 最後一行以粗體標示:"
    "「**本報告由 AI 產生,僅供研究參考,非投資建議。**」\n"
    "- 全文約 500~900 字,金融術語使用台灣慣用語"
    "(殖利率、波動度、回撤、大盤等)。"
)


async def get_current_regime(db: AsyncSession) -> dict[str, Any]:
    """Current market regime labels from the existing classifier.

    Asks `classify_regimes` for the last ``_REGIME_LOOKBACK_DAYS`` and
    keeps the regimes whose band reaches the latest data day — those
    are "active now". Regimes can overlap (bull + low_vol is a normal
    combination). Returns ``{"regimes": []}`` when the classifier has
    no data (e.g. empty archive in tests) so the prompt degrades to
    「資料不足」 instead of failing the review.
    """
    from services.regime_classifier import classify_regimes

    end = date.today()
    start = end - timedelta(days=_REGIME_LOOKBACK_DAYS)
    try:
        bands = await classify_regimes(db, market="TW", start=start, end=end)
    except Exception:
        log.warning("portfolio_review.regime_failed", exc_info=True)
        bands = []
    if not bands:
        return {"market": "TW", "as_of": None, "regimes": [], "regimes_zh": []}
    latest = max(b["end"] for b in bands)
    active = sorted({b["regime"] for b in bands if b["end"] == latest})
    return {
        "market": "TW",
        "as_of": latest,
        "regimes": active,
        "regimes_zh": [_REGIME_LABELS_ZH.get(r, r) for r in active],
    }


async def assemble_review_context(
    db: AsyncSession,
    *,
    portfolio_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Gather everything the review prompt needs.

    Risk numbers come from the SAME service call the C1 risk endpoint
    uses (weights/VaR/metrics/correlation/warnings in one payload — no
    math duplicated here); the regime label comes from the timeline
    classifier. Raises ``ValueError("Portfolio not found")`` for a
    missing or foreign portfolio, exactly like the /risk endpoint.
    """
    from services.portfolio_risk_service import get_portfolio_risk

    risk = await get_portfolio_risk(portfolio_id, user_id, db)
    regime = await get_current_regime(db)
    return {"risk": risk, "market_regime": regime}


def build_review_messages(
    ctx: dict[str, Any], *, portfolio_name: str,
) -> list[dict[str, str]]:
    """System + user message pair for the single-call review LLM."""
    ctx_json = json.dumps(ctx, ensure_ascii=False, default=str)
    user = (
        f"請為投資組合「{portfolio_name}」進行健檢。\n\n"
        f"<portfolio_context>\n{ctx_json}\n</portfolio_context>"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
