"""Prompt templates + per-block schema annotation for discussion LLM
calls.

All the static strings the persona / synthesizer LLM calls assemble
into prompts: the per-block annotation dict that explains every ctx
section to the LLM, the schema header it sits under, the per-turn
template each persona's system+user message is built from, and the
synthesizer system + user templates.

Plus the one pure helper that uses them — ``_persona_schema_annotation``
projects ``_BLOCK_ANNOTATIONS`` down to the blocks actually present
in the persona's filtered ctx so the LLM doesn't get told about a
``top_gainers`` block its ctx didn't carry.

Pure (no DB / LLM / I/O). discussion_service re-exports every name
for back-compat with the call sites that still live in the monolith
(``_ask_persona`` reads ``_TURN_PROMPT_TEMPLATE`` +
``_persona_schema_annotation``; ``synthesize_conclusion`` reads
``_SYNTHESIZER_SYSTEM`` + ``_SYNTHESIZER_USER_TEMPLATE``).
"""
from __future__ import annotations

from typing import Any


# Per-block schema annotation. Keys match top-level ctx field names so
# `_persona_schema_annotation()` can project the right subset for each
# persona. Without this annotation, weak models (Haiku, GPT-4o-mini,
# Llama-3.3) fixate on `top_gainers` and ignore the rest — defeating
# the cost of all the ingest crons feeding the context.
#
# Bullets are intentionally one-per-block. The `top_gainers` /
# `top_losers` pair shares one entry because they're rendered together;
# `risk_warnings` keeps its emphasis on the negative-filter semantics
# (that's where weak models most often go wrong).
_BLOCK_ANNOTATIONS: dict[str, str] = {
    "captured_session": (
        "- captured_session：**本次討論所有報價／技術指標／籌碼數據的真實基準日**。"
        "`session_date` = 多數區塊資料所屬的交易日；`phase` 描述本次討論所處時段"
        "（`intraday` 盤中 / `today_close_published` 今日已公布收盤 / "
        "`between_close_and_publish` 已收盤待 STOCK_DAY_ALL 更新 / "
        "`pre_open_today` 盤前 / `weekend_or_holiday` 非交易日 / `backtest` 回測模式）；"
        "`hint_zh` 是給你看的「現在這份 ctx 真實反映的是哪天的盤」說明。"
        "**禁止把 `session_date` 之後尚未發生的交易日當成已知資訊引用**——"
        "如 `phase=intraday` 表示今日盤面尚未收盤，不可說「今日 +X%」這類斷言；"
        "個別行的 `as_of_session` 若與 `captured_session.session_date` 不一致時，"
        "以該行自己的 `as_of_session` 為準。"
    ),
    "data_gaps": (
        "- data_gaps：**本次完全取不到資料的區塊名單**（上游斷線、端點改版、"
        "或該資料當日未發布）。名單上的區塊不會出現在 ## 市場現況 裡，"
        "**嚴禁**為它們寫出任何具體數值——沒有資料就直說沒有資料，"
        "並改用其他仍有資料的訊號替代推論。空陣列代表所有追蹤區塊都有資料。"
    ),
    "data_stale": (
        # Braces are doubled: this string is inlined into
        # `_TURN_PROMPT_TEMPLATE` / `_SYNTHESIZER_USER_TEMPLATE`, both
        # of which go through `str.format`.
        "- data_stale：**有資料但落後本場基準日的區塊**，格式為 "
        "`{{區塊: {{as_of, days_behind}}}}`。這些數字是 `as_of` 那天的事實，"
        "不是今天的。引用時必須連日期一起講（「分點資料 07/07（落後 14 天）"
        "顯示…」），並在推論時折價看待；落後越多、參考價值越低。"
        "**不得**把它當成當日最新狀態陳述。"
    ),
    "top_gainers": (
        "- top_gainers / top_losers：**`as_of_session` 標示之交易日**的漲跌幅前 10"
        "（動能 + 籌碼面）。注意：TWSE `STOCK_DAY_ALL` 端點要等 14:30 後才會更新"
        "今日收盤，所以盤中 (phase=intraday) 拿到的是昨日收盤排行，不是今日盤中。"
    ),
    "index": (
        "- index：大盤 (TAIEX) 報價 + 30 日歷史，用以判斷市場 regime。"
        "`is_intraday=true` 時 `value`／`change_pct` 是真正盤中即時數值（`MI_5MINS` 5 分鐘更新），"
        "`is_intraday=false` 時則為最近一次完成的收盤。`history_last_session` 是 30 日線最後一根 bar 日期。"
    ),
    "news_sentiment":            "- news_sentiment：所屬市場整體新聞情緒（bullish/bearish/neutral 計數，全視窗統計而非 headlines 抽樣）。",
    "per_symbol_news_sentiment": "- per_symbol_news_sentiment：主題提及之個股新聞情緒。",
    "short_term_signals": (
        "- short_term_signals：focus_symbols 的 Tier-1 短線技術訊號 "
        "(1-5 日視窗)。**每檔的 `as_of_session` 是計算用最後一根 bar 的日期，"
        "若早於 `captured_session.session_date` 表示這些技術指標尚未納入最新一日盤面**——"
        "RSI / MACD / gap_pct / return_5d 都是基於該日收盤算出。"
        "`volume_ratio`（量能 / 20 日均量倍數，>2 = 突破型）、"
        "`return_5d` / `return_20d`（短中期報酬 %）、`rsi_14`（< 30 超賣 / > 70 超買）、"
        "`gap_pct`（今日開盤跳空 %）、`kd_k` / `kd_d`（KD 9-3-3，K 從下方穿越 D"
        "且 < 20 = 偏多反轉，從上方穿越 D 且 > 80 = 偏空反轉）、"
        "`macd` / `macd_signal` / `macd_hist`（MACD 12-26-9，histogram 由負轉正 "
        "= 偏多動能起步、由正轉負 = 偏空動能起步）、"
        "`bollinger_pct_b` / `bollinger_width`（布林通道 20-2σ，%B > 1 = 突破上軌、"
        "< 0 = 跌破下軌；width 縮小 = 波動收斂常為突破前兆）、"
        "`obv_5d_change_pct`（OBV 5 日累計變化，正值且漲 = 籌碼增持確認、"
        "負值但漲 = 量價背離警示）、"
        "`holdings_concentration_trend`（個股大戶持股集中度 4 週趨勢："
        "`latest_top_holders_pct` 千張大戶 + 集中持有者累計持股比、"
        "`change_pp` 4 週百分點變化、`trend` rising/stable/falling — "
        "rising = 機構長線加碼，是與短線量能訊號獨立的籌碼確認）、"
        "`industry_rs`（同產業 RS：`symbol_return_5d` vs `industry_median_5d`，"
        "正值 = 領先類股，負值 = 落後）、"
        "`day_trading_trend`（個股當沖比 5 日趨勢：`latest_ratio` 最新當沖佔成交比、"
        "`mean_ratio` 5 日均值、`trend` rising/stable/falling — `latest_ratio > 0.5` "
        "且 `trend=rising` = 散戶亢奮過熱，短線見頂風險升）、"
        "`securities_lending_trend`（個股借券餘額 5 日趨勢：`latest_balance` 借券餘額、"
        "`balance_change_5d` 5 日變化（正值 = 餘額上升）、`trend` rising/stable/falling"
        " — `trend=rising` 代表機構透過借券建立隱性空頭部位，是 explicit 短賣以外的"
        "看空 leading flow，逆勢做多需特別留意）、"
        "`upcoming_event`（未來 14 日法說 / 除息行事曆：`next_event` earnings|ex_dividend、"
        "`next_event_in_days` 距事件日數 — `next_event_in_days <= 3` 屬事件窗，個股價格"
        "易出現 asymmetric move，**短線方向訊號失效**，建議 personas 切換為「停看聽」"
        "策略而非追多殺多）。配合 news_sentiment 同向時短線勝率較高。"
    ),
    "international_sentiment":   "- international_sentiment：Fed / FOMC / 國際宏觀新聞情緒，影響台股風險偏好。",
    "top_foreign_buyers":        "- top_foreign_buyers：近 5 日外資累計淨買超前 10 名（已含產業別）。",
    "taifex_positioning": (
        "- taifex_positioning：**外資台指期未平倉** (smart-money 方向訊號)。"
        "`fini.net_oi`（外資多單 - 空單，正值 = 淨多）、"
        "`fini.change_5d`（5 日變化，> +1000 口偏多 / < -1000 口偏空）、"
        "`trend` 已自動分類 bullish/bearish/neutral。**外資台指期方向往往領先大盤 1-2 日**，"
        "若與 news_sentiment 同向則短線方向確立度高；逆向則優先信任此訊號。"
    ),
    "single_stock_futures_oi": (
        "- single_stock_futures_oi：**個股期貨外資未平倉變化前 N 名** "
        "(per-stock smart-money 方向訊號，PR #282)。每筆含 `symbol` / "
        "`contract_id` / `fini_net_oi`（最新淨倉，正 = 淨多）/ "
        "`fini_change`（5 日淨倉變化，正 = 外資多單建倉，負 = 空單建倉）/ "
        "`industry` / `name_zh`。**個股期貨外資方向通常領先個股現貨 1-2 日**，"
        "與 top_foreign_buyers（現貨）同向 = 短線確立度極高，逆向 = 優先信任期貨方。"
        "引用時請帶具體口數（例：「外資個股期 2330 連 5 日多單 +1500 口」）。"
    ),
    "taiwan_vix": (
        "- taiwan_vix：**臺指選擇權波動率指數**（VIX_TW，PR #283）。"
        "`value` = 最新收盤、`change_pct` = 5 日變化 %。"
        "與 overseas_indicators 的 `^VIX`（美 VIX）並列：兩者方向 / 強度差距"
        "反映台美避險溢價 spread，VIX_TW 跳升而 ^VIX 平靜 = 台股 idiosyncratic "
        "風險偏高（地緣 / 央行 / 個股突發）；同步跳升 = 全球 risk-off。"
        "**> 25 視為高度恐慌**（歷史中位數約 16-18），結論時應提及避險建議。"
        "引用時請帶具體值（例：「台 VIX 22.3 + 5d +18%，避險溢價擴大」）。"
    ),
    "upcoming_events_calendar": (
        "- upcoming_events_calendar：**未來 30 日法說會 / 除權息行事曆**"
        "（PR #284，市場層級，限本次討論已關注的標的）。每筆含 `symbol` / "
        "`next_event`（earnings / ex_dividend）/ `next_event_in_days`（距今天數）/ "
        "`next_event_date`。**事件前後幾日波動放大、方向難測**，分析師應主動避開"
        "或為事件風險預留調整空間（停損 / 部位縮減 / 套保）。"
        "引用時請點名具體標的 + 距今天數（例：「2330 法說 3 日後，建議事件前減倉」）。"
    ),
    "broker_concentration": (
        "- broker_concentration：**主力分點 5 日累計買賣超**"
        "（PR #285，每筆即一個 focus_symbol）。每筆含 `symbol` / "
        "`top_buyers`[broker, broker_id, net_buy_shares] / `top_sellers`[…] / "
        "`session_count`。**branch-dot pattern 是公司派 / 主力 / 大戶在做什麼的"
        "最直接訊號** — 同一家分點連續多日大買大賣，往往領先個股 3-5 日大行情。"
        "若 top_buyers / top_sellers 集中度高（前 3 大佔總額 > 50%），代表"
        "走勢有 single-actor driver，分析時應將該分點當作主軸；分散度高則"
        "表示資金無共識，技術分析權重應降低。引用時請點名具體分點 + 張數"
        "（例：「凱基台北 連 5 日淨買 +50K 張」），不要只說「主力大買」。"
    ),
    "margin_balance_trend":      "- margin_balance_trend：全市場融資 / 融券餘額趨勢（散戶槓桿與看空代理）。",
    "top_revenue_growers":       "- top_revenue_growers：最新月份營收年增率前 10（基本面）。",
    "active_buybacks":           "- active_buybacks：今日仍在執行庫藏股的公司，**強烈管理層信心訊號**。",
    "govt_bank_flow_5d":         "- govt_bank_flow_5d：八大行庫近 5 日累計買賣超（國家隊方向）。",
    "risk_warnings": (
        "- risk_warnings：**負向過濾**——`active_dispositions`（處置股）、"
        "`recent_suspensions`（近期暫停交易）、`high_day_trading_ratio`"
        "（當沖比 >60%，投機過熱）。**禁止推薦中招的標的，即使其他訊號看多。**"
    ),
    "market_institutional_5d":   "- market_institutional_5d：全市場三大法人近 5 日淨買賣超（大盤方向）。",
    "focus_briefs": (
        "- focus_briefs：**主題提及之個股小型分析師簡報**——`quote`（含 `as_of_session` "
        "標示報價所屬交易日 + `is_intraday` 是否盤中即時）、"
        "`technicals`（MA20/60/120、52w 高低與距離、5/20/60 日漲跌幅、RSI14，也帶 `as_of_session`）、"
        "`fundamentals`（PE/PB/殖利率/EPS）、`revenue_trend`（近 6 月營收年/月增）、"
        "`chip_5d`（外資 / 投信 / 自營近 5 日淨買賣）、`margin_latest`（最新融資餘額）、"
        "`peers`（同產業 3 檔可比標的）。**有此區塊就要引用具體數據**，"
        "不要只憑 headlines 推論。**引用報價時要連帶說明 `as_of_session`** "
        "（例：「2330 以 5/27 收盤 1,100 元為基準」），不要只說「目前股價」。"
    ),
    "macro": (
        "- macro：宏觀利率與匯率快照（Fed Funds / US 10Y / 10Y-2Y 殖利率價差 / "
        "DXY / TWD/USD），各帶 `latest_value` + `change_1y` + `change_3m`。"
        "影響全球風險偏好與外資流向，建議在結論中至少提及一次相關方向。"
    ),
    "overseas_indicators": (
        "- overseas_indicators：**隔夜美股 / 全球指數快照**（PR #269）——"
        "`indices` 內含 ^SOX (費半) / ^IXIC (NASDAQ) / ^GSPC (S&P 500) / "
        "^DJI (道瓊) / ^VIX (波動率指數)，各帶 `close` + `prev_close` + "
        "`change_pct`。**台股開盤前必看** — `^SOX` 對台積電等晶圓代工股有 1-2 日領先性，"
        "`^VIX` 跳升代表全球避險情緒升溫應降低部位風險。引用時請帶具體 % 變化"
        "（例如「SOX -2.3% 拖累半導體」），勿只提名稱。"
    ),
    "user_context": (
        "- user_context：**討論發起人本人的部位**——`portfolios`（組合清單）、"
        "`holdings`（前 20 大持股，含股數 / 平均成本 / 計價幣別）、"
        "`watchlist_symbols`（自選股，前 30）、`focus_overlap.held` "
        "（主題提及的標的中已持有者）、`focus_overlap.watching`（自選股中相關者）。"
        "**只在你的角色與部位配置 / 風險管理相關時引用**；其他情境忽略。"
        "**禁止在結論中揭露具體股數或成本價**——僅用於決策邏輯。"
    ),
    "prior_discussions": (
        "- prior_discussions：**本人過去 90 天對主題提及之標的所做的結論**，含 "
        "`created_at`（建立時間）/ `as_of_date`（回測日期，回測討論才有；"
        "live 為 null）/ topic 摘要 / `recommended_symbols` / `time_horizon` / "
        "`consensus_score` / `verdict`（win / loss / unverifiable / null）。"
        "**用以保持跨討論一致性**——若上次對 2330 結論 Hold 而本次卻要 Buy，"
        "必須在 content 中明確說明改變理由（例如「上週起殖利率下行 +30bp，重新評估」），"
        "不可默默翻盤。引用過去討論時請優先以 as_of_date（若有）為錨，更貼近「在那一天的判斷」。"
    ),
    "recent_lessons": (
        "- recent_lessons：**過去事後檢討學到的教訓**——`market` 為全市場通用教訓"
        "（time-decay 排序），`per_symbol` 為主題提及之標的的歷史教訓"
        "（同 symbol 加權）。每條含 `as_of_date` / `category` "
        "(missed_sector | wrong_signal_weight | missing_data | "
        "over_confidence | other) / `lesson_text` (≤ 80 字反思) / "
        "`related_symbols` / `missed_winners`。**這是「上次踩坑的具體紀錄」**——"
        "回應前先掃過，避免重蹈覆轍；若這次的論點正好在 lesson_text 警示的"
        "範圍內，必須明確處理（要麼提出新證據反駁，要麼承認並調整）。"
        "若 lessons 為空表示尚未累積足夠歷史教訓，正常進行即可。"
    ),
    "errors":                    "- errors：本次抓取的連接器錯誤清單；非空時務必聲明資料不完整。",
}

_SCHEMA_HEADER = (
    "## 市場現況解讀提示\n"
    "下方 `## 市場現況` 的 JSON 包含多個訊號區塊，請依語意整合判讀：\n"
)


def _format_freshness_preamble(ctx: dict[str, Any]) -> str:
    """Build the '## 資料時效' block placed at the top of every persona
    + synthesizer prompt. Reads `ctx['captured_session']` so the LLM
    sees the actual session anchor in plain prose right before it sees
    the JSON ctx — same data the per-block annotation also describes,
    but lifted up so weak models (Haiku / Llama-3.3 / GPT-4o-mini)
    don't miss it buried in the JSON.

    Falls back gracefully when an older ctx (pre-Phase-1) lacks the
    `captured_session` block — emits a generic notice so the prompt
    still renders.
    """
    sess = ctx.get("captured_session") or {}
    session_date = sess.get("session_date")
    phase = sess.get("phase")
    is_intraday = sess.get("is_intraday")
    hint = sess.get("hint_zh") or ""
    if not session_date and not hint:
        return (
            "本次 ctx 未攜帶 captured_session 元資料；"
            "請以個別區塊的 `as_of_session` 為準判讀。"
        )
    lines = []
    if session_date:
        lines.append(f"- **基準交易日 (session_date)**：{session_date}")
    if phase:
        lines.append(f"- **時段標籤 (phase)**：{phase}")
    if is_intraday is not None:
        lines.append(f"- **是否盤中即時 (is_intraday)**：{is_intraday}")
    if hint:
        lines.append(f"- **說明**：{hint}")
    lines.append(
        "- **準則**：不要把 `session_date` 之後尚未發生的盤面當成"
        "已知資訊。若 phase=`intraday`，禁止以「今日漲了 X%」"
        "這種已實現語氣描述今日；改用「截至昨日收盤 + 今日盤中 TAIEX "
        "走勢」這種精確措辭。各區塊行內的 `as_of_session` 與本基準"
        "不一致時，以該行為準。"
    )
    return "\n".join(lines)


def _persona_schema_annotation(ctx: dict[str, Any]) -> str:
    """Build a schema annotation listing only the blocks present in
    `ctx`. Avoids the failure mode where the prompt advertises
    `top_gainers` / `risk_warnings` to a `macro_analyst` whose
    filtered ctx didn't carry them — the LLM would either invent
    values or apologise about missing data, both equally bad.

    Block ordering follows `_BLOCK_ANNOTATIONS` insertion order so
    the prompt structure stays stable across personas. A block is
    "present" if its key is in `ctx` AND the value isn't None / [] /
    {} (matches how `gather_market_context` initialises empty
    blocks). `errors` is always included so personas know what
    `errors: []` means even on a clean run.
    """
    bullets: list[str] = []
    for block, line in _BLOCK_ANNOTATIONS.items():
        if block == "errors":
            bullets.append(line)
            continue
        val = ctx.get(block)
        if val is None or val == [] or val == {}:
            continue
        bullets.append(line)
    return _SCHEMA_HEADER + "\n".join(bullets)


# Full annotation kept for `synthesize_conclusion` (which sees the
# unfiltered context snapshot, so the full block list is always
# accurate). Built once at import time.
_CONTEXT_SCHEMA_ANNOTATION = _SCHEMA_HEADER + "\n".join(
    _BLOCK_ANNOTATIONS.values()
)


_TURN_PROMPT_TEMPLATE = (
    "你正在參加一場專家圓桌討論。你的角色身份請依系統提示扮演。\n\n"
    "## 語言規範（最重要）\n"
    "整段 content **必須用繁體中文（台灣用語）**。\n"
    "  - 用「漲停」不用「涨停」、用「資金」不用「资金」、用「電子」不用「电子」。\n"
    "  - 金融術語照台灣慣用：殖利率 / 本益比 / 三大法人 / 月營收年增。\n"
    "  - 不要混入簡體字，即使你的訓練資料偏向簡體也要轉繁。\n\n"
    "## 資料時效（必讀）\n"
    "{freshness_preamble}\n\n"
    "## 主題\n{topic}\n\n"
    "## 共同規則\n{rules}\n\n"
    "{annotation}\n\n"
    "## 市場現況\n```json\n{context}\n```\n\n"
    "## 訊號引用準則（高優先）\n"
    "為避免「資料看了沒用、結論自由心證」，content 中引用 ## 市場現況 的訊號時"
    "必須遵守以下三條：\n"
    "  1. **引用具體數值**，不要只用一般性形容詞。\n"
    "     ✗ 「RSI 偏高」、「外資動向轉弱」\n"
    "     ✓ 「RSI 67.3 已逼近超買區」、「外資台指期淨空 5 日 +3500 口」\n"
    "  2. **每個與本主題相關的訊號區塊都要至少點名一次**（即使結論是「該訊號"
    "與本案無顯著關聯」也要明寫，不要默默跳過）。判斷相關性的標準：annotation"
    "中該區塊的描述若觸及主題提到的個股 / 產業 / 時間視窗，即屬相關。\n"
    "  3. **訊號之間互相印證或衝突要明寫**。例：「news_sentiment 偏多但"
    "taifex_positioning trend=bearish — 兩者相左，優先信任後者，因外資期貨"
    "部位通常領先大盤 1-2 日」。單一訊號可被忽略，多訊號互證或互斥不可。\n"
    "  4. **只能引用 ## 市場現況 裡真的存在的數字**。上面沒有給你的資料，"
    "一律不得寫出具體數值——包含你從訓練資料記得的、或前幾輪別人講過但 ctx "
    "查不到的。`data_gaps` 列出的區塊本次完全沒有資料，正確的寫法是"
    "「本場無 VIX 讀數，改以 taifex_positioning 判斷風險」，"
    "**不是**憑印象補一個數字上去。第 1 條要求引用具體數值，指的是引用"
    "**ctx 內**的具體數值；資料缺漏時，誠實說缺比湊一個數字有價值得多。\n\n"
    "## 先前發言\n{history}\n\n"
    "## 你現在的任務\n"
    "依照你扮演的角色立場，閱讀上述資料與先前發言後，"
    "**直接輸出合法 JSON**（不要包 markdown code fence、不要在 JSON 之前或之後加任何解釋文字）：\n"
    '{{"stance": "agree|dissent|supplement", "content": "你的發言"}}\n\n'
    "stance 規則：\n"
    "  - agree：完全同意先前共識，無新內容可補充。content 可留空或一句話致意。\n"
    "  - dissent：對某位專家的觀點有具體反對，必須點名是反對誰、為什麼。\n"
    "  - supplement：補充新資訊、新角度、新數據。\n\n"
    "## 排版規範\n"
    "為了讓使用者快速抓重點，content 內請用 markdown 強調語法：\n"
    "  - 關鍵結論、重要數字、目標價、停損點、股票代號 → 用 **粗體**\n"
    "    例如：**台積電 (2330)**、**目標價 650 元**、**停損 565 元**、**+5.2%**\n"
    "  - 段落之間用空行分開，不要寫成一整塊牆。\n"
    "  - 條列重點時用 `- ` 開頭，每點獨立一行。\n"
    "  - **不要**用 markdown 標題 (`#`、`##`),也不要用程式碼區塊。\n\n"
    "content 必須遵守共同規則中的字數限制與引用要求。"
)


_SYNTHESIZER_SYSTEM = (
    "你是一位資深投資組合經理，主持本場圓桌討論。"
    "你的任務是閱讀全部專家發言，整理出可執行的結論。"
    "你不偏袒任何一位專家，而是從他們的共識與分歧中抓出最高勝率的觀點。\n"
    "**所有輸出必須使用繁體中文（台灣用語）**，禁用簡體字。"
)

_SYNTHESIZER_USER_TEMPLATE = (
    "## 討論主題\n{topic}\n\n"
    "## 討論規則\n{rules}\n\n"
    "## 資料時效（必讀）\n{freshness_preamble}\n\n"
    + _CONTEXT_SCHEMA_ANNOTATION + "\n\n"
    "## 市場現況\n```json\n{context}\n```\n\n"
    "## 全部發言（依序）\n{transcript}\n\n"
    "## 任務\n"
    "**直接輸出合法 JSON**（不要包 markdown code fence、不要在 JSON 之前或之後加任何解釋、"
    "不要寫 // 或 /* */ 註解）：\n"
    "{{\n"
    '  "recommendations": [\n'
    '    {{"symbol": "2330", "confidence": 0.75}},\n'
    '    {{"symbol": "0050", "confidence": 0.55}}\n'
    "  ],\n"
    '  "reasoning": "結論摘要，≤200字，繁體中文，引用至少2位專家",\n'
    '  "risks": ["風險1", "風險2"],\n'
    '  "time_horizon": "short_term",\n'
    '  "consensus_score": 0.7,\n'
    '  "abstained": false,\n'
    '  "abstain_reason": ""\n'
    "}}\n\n"
    "欄位規則：\n"
    "- abstained：本場是否棄權（不推薦任何標的）。候選股全都不符合進場條件時，"
    "請誠實填 true、recommendations 留空陣列，並在 abstain_reason 說明棄權原因。"
    "**棄權是合法且被鼓勵的結論** —— 空頭或訊號不明時硬湊推薦，比棄權更糟。"
    "只要 recommendations 非空，abstained 就必須是 false。\n"
    "- recommendations：最多 5 檔，要有市場共識且風險可控。每檔包含：\n"
    "  - symbol：股票代號（字串）\n"
    "  - confidence：0.0-1.0，你對這檔在「第 5 個交易日收盤相對進場日開盤有顯著正向報酬」的把握程度"
    "（以第 5 日收盤結算，期間曾經漲過又跌回來不算）。"
    "請保守估計：有重大不確定性給 0.5 以下；極度有把握再給 0.8 以上；"
    "全 conclusion 不應全部 ≥ 0.8（這代表沒有風險意識）。\n"
    "- time_horizon：只能是 short_term / medium_term / long_term 三選一\n"
    "- consensus_score：0.0 到 1.0 之間的數字，0 代表完全分歧，1 代表完全共識\n"
)
