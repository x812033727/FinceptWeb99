"""One-shot seeder: creates 6 well-known trader strategy templates
under a target user (default: the admin user from `ADMIN_EMAIL`).

The 6 strategies cover the major spectrum of professional trading
styles — short-term momentum, macro trend-following, mean-reversion,
deep value, multi-factor quant, and contrarian / distressed:

  1. 動量突破 (Momentum Breakout)            — TW   — Minervini + Livermore
  2. 宏觀趨勢追蹤 (Macro Trend Following)    — GLOBAL — PTJ + Dalio
  3. 均值回歸拉回 (Mean Reversion Pullback)  — TW   — Raschke
  4. 價值投資護城河 (Value Investing Moat)   — US   — Buffett + Graham + Munger
  5. 量化多因子 (Quant Multi-Factor)         — US   — Simons + Asness
  6. 反向危機投資 (Contrarian Distressed)    — GLOBAL — Klarman + Marks + Soros

Each maps to existing personas in `ai/agents.py` and uses the validated
`strategy_template_service.create_template` path so bounds (rounds 1-5,
concurrency 1-3, market in {TW,US,GLOBAL}) are enforced at insert.

Run from inside the backend dir:

    # default — seeds under settings.ADMIN_EMAIL
    python -m scripts.seed_trading_strategies

    # seed under a different user
    python -m scripts.seed_trading_strategies --email user@example.com

Idempotency: skips strategies whose `name` already exists for the
owner (active templates only — soft-deleted ones don't block a
re-create). Re-running the script after manually deleting a template
will recreate it; re-running the script untouched is a no-op.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from ai.agents import all_persona_ids
from config import settings
from db.session import AsyncSessionLocal
from models.user import User
from services import strategy_template_service as svc

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrategyPreset:
    name: str
    description: str
    topic: str
    rules: str
    market: str
    persona_ids: tuple[str, ...]
    default_rounds: int = 2
    default_concurrency: int = 1


TRADING_STRATEGIES: tuple[StrategyPreset, ...] = (
    StrategyPreset(
        name="動量突破 (Momentum Breakout)",
        description=(
            "Mark Minervini SEPA 模式 + Jesse Livermore 樞軸點突破。"
            "鎖定高量突破基底的成長股,8% 鐵律停損,順勢加碼。"
        ),
        topic=(
            "今日台股是否有符合 SEPA 模式或樞軸點突破的進場標的?"
            "重點關注 5%+ 漲幅且成交量 ≥ 5 倍均量的中小型成長股,"
            "並評估其相對大盤強度與基底完整性。"
        ),
        rules=(
            "進場條件:\n"
            "  - 股價突破 50 日基底高點 (cup-with-handle / VCP 形態)\n"
            "  - 突破日成交量 ≥ 5 倍 50 日均量\n"
            "  - 5/20/50/200 MA 多頭排列\n"
            "  - 距離 52 週高點 ≤ 25%\n"
            "  - 相對強度 RS > 70\n"
            "停損:\n"
            "  - 進場價 -8% 強制出場 (Minervini 鐵律)\n"
            "  - 跌破 20 MA 二次部分減碼\n"
            "加碼 (Livermore 樞軸點再進場):\n"
            "  - 突破後拉回不破 8MA 可加碼\n"
            "避開:\n"
            "  - EPS 連 2 季衰退\n"
            "  - 業外或公司治理疑慮"
        ),
        market="TW",
        persona_ids=("minervini", "livermore", "market_analyst", "trading_coach"),
    ),
    StrategyPreset(
        name="宏觀趨勢追蹤 (Macro Trend Following)",
        description=(
            "Paul Tudor Jones + Ray Dalio 風格。從利率/通膨/匯率/景氣循環"
            "判斷主要資產類別 (股債商品匯) 的趨勢方向,順勢進場、嚴控風險。"
        ),
        topic=(
            "目前全球宏觀環境 (Fed 政策、CPI、GDP、殖利率曲線、美元指數) "
            "指向哪些資產類別處於主要趨勢?應如何配置股 / 債 / 商品 / 匯?"
        ),
        rules=(
            "趨勢判定:\n"
            "  - 月線 / 週線方向一致 (200/50 SMA 順勢排列)\n"
            "  - 宏觀邏輯支撐 (Fed 方向、實質利率、信用利差)\n"
            "  - 跨資產確認 (股債商匯互相驗證)\n"
            "進場:\n"
            "  - 多頭趨勢且拉回未破 50 SMA\n"
            "  - 風險報酬比 ≥ 3:1\n"
            "停損:\n"
            "  - 跌破關鍵均線 / 支撐 → 立即出場,絕不凹單\n"
            "  - 單筆部位風險 ≤ 帳戶 1-2% (PTJ 紀律)\n"
            "避開:\n"
            "  - 趨勢轉折期 (盤整、訊號矛盾)\n"
            "  - 宏觀邏輯與走勢背離"
        ),
        market="GLOBAL",
        persona_ids=("ptj", "dalio", "macro_analyst", "risk_manager"),
    ),
    StrategyPreset(
        name="均值回歸拉回 (Mean Reversion Pullback)",
        description=(
            "Linda Raschke 短線風格。多頭趨勢中等待健康拉回 (3-5 日連跌"
            "或回測 20 MA),反彈出場,絕不凹單。"
        ),
        topic=(
            "台股目前哪些強勢標的剛經歷 3-5 日健康拉回,"
            "具備短線反彈進場條件?關注 RSI(2) 與 K(9) 超賣信號。"
        ),
        rules=(
            "趨勢確認:\n"
            "  - 200 SMA 與 50 SMA 多頭排列\n"
            "  - 過去 20 個交易日相對大盤強勢 (跑贏 5%+)\n"
            "進場:\n"
            "  - 連 3 日下跌或回測 20 SMA / 上升趨勢線\n"
            "  - RSI(2) < 20 或 K(9) < 20 反彈\n"
            "  - 進場日當沖或隔日開盤\n"
            "停損:\n"
            "  - 進場價 -3% 或跌破近期低點\n"
            "  - 持倉時間 ≤ 5 個交易日\n"
            "獲利了結 (Raschke anti 法則):\n"
            "  - 反彈至前高或 +5% 分批出場\n"
            "  - 不貪心,優先確保勝率"
        ),
        market="TW",
        persona_ids=("raschke", "market_analyst", "trading_coach"),
    ),
    StrategyPreset(
        name="價值投資護城河 (Value Investing Moat)",
        description=(
            "Buffett + Graham + Munger 風格。長期持有具經濟護城河、"
            "ROIC 高、價格低於內在價值且具安全邊際的優質公司。"
        ),
        topic=(
            "美股目前有哪些具備寬護城河、ROIC > 15%、"
            "PE 低於十年中位數,且具備 30%+ 安全邊際的進場機會?"
        ),
        rules=(
            "品質指標 (Buffett / Munger):\n"
            "  - ROIC ≥ 15% 且持續 10 年\n"
            "  - 自由現金流為正且穩定成長\n"
            "  - 護城河來源明確 (品牌、規模、轉換成本、專利)\n"
            "  - 經營階層誠信、資本配置合理\n"
            "估值 (Graham 安全邊際):\n"
            "  - PE 或 EV/EBIT 低於十年中位數 25%+\n"
            "  - DCF 內在價值高於現價 30%+\n"
            "避開:\n"
            "  - 高槓桿 (Net Debt / EBITDA > 3x)\n"
            "  - 會計疑慮 / 商譽佔比過高\n"
            "  - 競爭格局劣化 (新進者破壞護城河)\n"
            "時間框架:\n"
            "  - 持有 3-5 年起跳,只在內在價值嚴重高估或基本面破壞時退場"
        ),
        market="US",
        persona_ids=("buffett", "graham", "munger", "earnings_analyst"),
    ),
    StrategyPreset(
        name="量化多因子 (Quant Multi-Factor)",
        description=(
            "Renaissance / AQR 風格。價值 / 動量 / 品質 / 低波四因子排序,"
            "多空配對,行業中性,風險控制優先。"
        ),
        topic=(
            "美股當前在價值、動量、品質、低波四因子上,"
            "哪些股票排名前十、後十?應如何長空配對?因子之間的相關性如何?"
        ),
        rules=(
            "因子定義:\n"
            "  - 價值: EBIT/EV, FCF Yield, Book/Price\n"
            "  - 動量: 12-1 月報酬, 6 月報酬\n"
            "  - 品質: ROE, 毛利率穩定性, 應計項佔比低\n"
            "  - 低波: 60 日波動率倒數\n"
            "組合:\n"
            "  - 四因子 z-score 等權加總,排序前 10% 做多、後 10% 做空\n"
            "  - 行業中性 (各 GICS 部門權重接近基準)\n"
            "  - 個股權重上限 2%\n"
            "風控:\n"
            "  - 每月再平衡\n"
            "  - 因子相關性監控 (價值與動量負相關時降低槓桿)\n"
            "  - 最大回撤 ≥ 12% 暫停加碼\n"
            "排除:\n"
            "  - 市值 < $1B\n"
            "  - 流動性 < 日均成交 $5M"
        ),
        market="US",
        persona_ids=("simons", "asness", "smith", "risk_manager"),
    ),
    StrategyPreset(
        name="反向危機投資 (Contrarian Distressed)",
        description=(
            "Howard Marks + Seth Klarman + Soros 風格。市場極度悲觀時"
            "逢低介入,要求安全邊際、不對稱報酬與多重獲利路徑。"
        ),
        topic=(
            "目前全球市場有哪些資產處於極度悲觀狀態 "
            "(forced selling、流動性恐慌、政策不確定),"
            "具備非對稱報酬機會?風險與催化劑為何?"
        ),
        rules=(
            "信號 (Marks 鐘擺):\n"
            "  - 投資人情緒指標 (AAII / Fear & Greed) < 20\n"
            "  - 信用利差擴張到歷史 80 百分位以上\n"
            "  - 強迫賣盤 (基金贖回、margin call、政策衝擊)\n"
            "Klarman 安全邊際:\n"
            "  - 帳面或清算價值高於現價 50%+\n"
            "  - 多重路徑獲利 (基本面修復、被併購、清算分配)\n"
            "  - 避免使用槓桿,保留乾火藥\n"
            "Soros 反身性:\n"
            "  - 認知與現實落差 (主流過度悲觀,但基本面已開始修復)\n"
            "  - 政策轉向訊號 (央行、財政、產業)\n"
            "風控:\n"
            "  - 單筆部位 ≤ 5% (危機資產相關性高)\n"
            "  - 願意持有 2-3 年等待修復\n"
            "  - 設定最大下檔 (-20%) 以再評估命題"
        ),
        market="GLOBAL",
        persona_ids=("klarman", "marks", "soros", "macro_analyst"),
    ),
)


def _validate_personas() -> None:
    """Fail fast if any preset references an unknown persona — prevents
    a typo from getting all the way to runtime where it would surface
    as a confusing 'unknown agent' error mid-discussion."""
    known = set(all_persona_ids())
    for s in TRADING_STRATEGIES:
        unknown = [p for p in s.persona_ids if p not in known]
        if unknown:
            raise ValueError(
                f"strategy {s.name!r} references unknown persona(s): "
                f"{unknown}. Known personas: {sorted(known)}"
            )


async def _resolve_owner(db, email: str) -> User:
    user = await db.scalar(select(User).where(User.email == email))
    if user is None:
        raise SystemExit(
            f"error: no user found with email {email!r}. "
            f"Use --email to pick a different target."
        )
    return user


async def seed_for_owner(db, owner_id: UUID) -> tuple[int, int]:
    """Create any missing strategy templates for `owner_id`. Returns
    `(created, skipped)`. Idempotent: a template whose `name` already
    exists for the owner (active only) is left untouched."""
    existing = {t.name for t in await svc.list_templates(db, owner_id=owner_id)}
    created = 0
    skipped = 0
    for strat in TRADING_STRATEGIES:
        if strat.name in existing:
            log.info("seed_strategies.skip_existing", extra={"name": strat.name})
            skipped += 1
            continue
        await svc.create_template(
            db,
            owner_id=owner_id,
            name=strat.name,
            description=strat.description,
            topic=strat.topic,
            rules=strat.rules,
            market=strat.market,
            persona_ids=list(strat.persona_ids),
            default_rounds=strat.default_rounds,
            default_concurrency=strat.default_concurrency,
        )
        created += 1
        log.info("seed_strategies.created", extra={"name": strat.name})
    return created, skipped


async def _run(email: str) -> int:
    _validate_personas()
    async with AsyncSessionLocal() as db:
        owner = await _resolve_owner(db, email)
        created, skipped = await seed_for_owner(db, owner.id)
    print(
        f"\n[ok] target={email} (id={owner.id})  "
        f"created={created}  skipped_existing={skipped}  "
        f"total_presets={len(TRADING_STRATEGIES)}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--email", default=settings.ADMIN_EMAIL,
        help="target user's email (default: settings.ADMIN_EMAIL)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not args.email:
        print(
            "error: --email is required when ADMIN_EMAIL is unset",
            file=sys.stderr,
        )
        return 2

    return asyncio.run(_run(args.email))


if __name__ == "__main__":
    raise SystemExit(main())
