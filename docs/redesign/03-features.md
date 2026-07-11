# 功能路線圖藍圖

> 本文件是[架構重規劃藍圖](00-overview.md)的功能章節。四大方向均以現有程式碼為基礎擴充(已逐一核對原始碼),避免重複造輪子。規模估計:S ≈ 1-3 天、M ≈ 1-2 週、L ≈ 2-4 週。

## A. 看盤與圖表進階

現況:`frontend/src/components/charts/CandlestickChart.tsx` 為單一 lightweight-charts v4 元件(K 線 + 量能疊加,無指標、無週期切換);`StockDetailPage.tsx` 以固定 360px 高度嵌入;資料僅有 `ohlcv_daily`(日 K)與 `quote_snapshots`(每 60 秒快照、30 天保留)。

| # | 功能 | 說明 | 建構基礎 | 新增介面 | 規模 |
|---|------|------|----------|----------|------|
| A1 | 技術指標疊加 | 在 K 線上疊加 MA/EMA/布林通道,副圖顯示 RSI/MACD/KD。指標於前端以純函式計算(日 K 資料量小),使用者可勾選開關並記憶偏好。 | `CandlestickChart.tsx`(`addLineSeries`、多 `priceScaleId` 副圖模式已有 vol 前例) | 新 `frontend/src/lib/indicators.ts`(純函式 + 單測)、`IndicatorToolbar.tsx`;偏好存 `localStorage`,無後端變更 | M |
| A2 | 多週期切換(日/週/月 + 分時) | 日→週/月由前端聚合日 K 即可;TW/US 分時線用 `quote_snapshots` 於後端聚合成 1m/5m bar(僅近 30 天,受保留政策限制,需在 UI 標示)。 | `models/ohlcv_daily.py`、`models/quote_snapshot.py`、`api/tw_market/router.py` `GET /history/{symbol}` | 後端新增 `GET /{market}/intraday/{symbol}?interval=1m|5m`(SQL `date_trunc` 聚合);前端 `TimeframeSelector.tsx` | M |
| A3 | 全螢幕看盤模式 | K 線區一鍵放大至全視窗,支援 ESC 退出與高度自適應(現為固定 360px)。低風險高感受度。 | `CandlestickChart.tsx`(已有 ResizeObserver)、`StockDetailPage.tsx` | 前端 Fullscreen API wrapper + `height` 改為容器驅動;無後端 | S |
| A4 | 比較圖(多標的相對績效) | 選 2-5 檔標的以基準日 =100 正規化疊線,支援跨市場(TW/US/CRYPTO)。可從 watchlist 直接帶入。 | `ai/tools/financial.py` 的 `compare_quotes` 概念、各 market `history` endpoint、`watchlist_service.py` | 前端 `ComparisonChart.tsx`(lightweight-charts 多 line series);後端可選 `GET /global/compare-history` 合併端點 | M |
| A5 | 畫線工具(趨勢線/水平位) | 手繪趨勢線、水平支撐壓力線,存至使用者帳號並跨裝置同步;水平線可一鍵轉為價格告警(串 D 方向)。 | lightweight-charts v4 無內建繪圖 — 需自製 overlay canvas 或升級 v5 + plugin(需評估) | 新表 `chart_drawings`(user_id, symbol, market, kind, points JSONB);`GET/PUT /charts/drawings`;前端 `DrawingLayer.tsx` | L |

依賴順序:A3 → A1 → A2 → A4 → A5(A5 依賴 A1 的圖層重構)。

## B. AI 能力強化

現況:`ai/llm_router.py`(8 供應商串流 + usage 記帳)、`ai/agents.py`(23 personas)、`ai/tools/`(quote/dcf/var/backtest/news/籌碼等 17 個 tool)、`services/discussion/`(圓桌 SSE、lessons、scoreboard、strategy template/sweep、auto-run 含 email 報告)。`api/ai_agents` 目前僅 `GET /agents` + `POST /chat`。

| # | 功能 | 說明 | 建構基礎 | 新增介面 | 規模 |
|---|------|------|----------|----------|------|
| B1 | 個股 AI 研究報告 | 從 StockDetailPage 一鍵產生單檔深度報告:估值(DCF)、籌碼、營收、新聞情緒、技術面,結構化輸出並可存檔/寄送。重用 discussion 的 context 組裝而非重寫。 | `services/discussion/context_assembly.py`、`technicals.py`、`ai/tools/financial.py`、`email_service.py` | `POST /ai/stock-report/{market}/{symbol}`(SSE);新表 `stock_reports`(快取 + 歷史);前端 `StockAIReportPanel.tsx` | M |
| B2 | 自然語言查詢(NL→篩選/圖表) | 對話框輸入「近月外資買超且 RSI<40 的電子股」,LLM 以 tool-use 轉為 screener 查詢 + 結果表格/圖表。建於既有 SQL tool 的安全框架上(唯讀白名單)。 | `ai/tools/sql.py`、`api/tw_market` screener、`ai/llm_router.py` tool_call 事件流 | 新 tool `run_screener`;前端 CommandPalette 整合或 `NLQueryBar.tsx`;無新表 | M |
| B3 | AI 選股助手 | 每日排程對 watchlist / screener 結果跑輕量多 persona 評分(重用 persona 權重學習與 confidence calibrator),輸出 top-N 候選與理由,進入 scoreboard 追蹤命中率。 | `services/persona_weight_learner.py`、`confidence_calibrator.py`、`discussion_scoreboard_service.py`、`tasks/auto_run_discussion.py` 排程模式 | 新任務 `tasks/ai_stock_pick.py`、新表 `stock_pick_runs`;`GET /ai/picks`;前端 `PicksPage` 區塊 | L |
| B4 | 圓桌討論深化:使用者插話與追問 | 討論進行中允許使用者注入問題,由 moderator 分派給指定 persona 回答;結論卡支援「追問」延伸回合。 | `services/discussion/round_runner.py`、SSE 通道、`conclusion_parsing.py` | `POST /discussion/{id}/interject`;`discussion_turns` 加 `injected_by_user` 欄位;前端 `DiscussionToolbar.tsx` 擴充 | M |
| B5 | AI 投組健檢(串 C 方向) | 以既有分析工具對使用者投組做一鍵 AI 評估:集中度、風險值、與 regime 的適配度,產出行動建議。 | `ExpertEvaluationCard.tsx`(前端已有雛形)、`analytics/risk.py`、`regime_classifier.py`、`portfolio_analytics.py` | 新 tool `get_portfolio_snapshot`(需 user-scoped 授權設計);`POST /ai/portfolio-review/{portfolio_id}` | M |

依賴順序:B1 → B2 → B4 → B5 → B3(B3 依賴 B1 的報告基建與 scoreboard 掛接)。

**風險註記**:B2 的 SQL tool 必須維持唯讀 + 資料表白名單;B5 的 tool 需綁定 user_id 防越權。

## C. 投組與回測強化

現況:`analytics/backtest.py`(日線事件驅動引擎,僅 2 內建策略,無做空/停損)、`analytics/risk.py`(三法 VaR + Sharpe/Sortino/Calmar/Beta)、`portfolio_optimizer.py`、`walk_forward_service.py`、`backtest_sweep_service.py` 已存在;`api/analytics` 僅 3 端點(dcf/var/backtest,皆無持久化);`api/portfolio` 有完整 CRUD + optimise + performance。

| # | 功能 | 說明 | 建構基礎 | 新增介面 | 規模 |
|---|------|------|----------|----------|------|
| C1 | 投組風險儀表板 | 把 `risk.py` 三法 VaR、beta、回撤直接算在使用者實際投組上(目前 `/analytics/var` 是無狀態的手動輸入),含相關性熱圖與集中度警示。 | `analytics/risk.py`、`services/portfolio_service.py`、`broker_concentration_service.py`、`tasks/portfolio_snapshot.py` | `GET /portfolio/{id}/risk`;前端 `RiskDashboardPanel.tsx`(Recharts 熱圖);無新表(即算) | M |
| C2 | 回測引擎擴充 | 加入停損/停利、部位權重、做空、滑價模型與更多內建策略(breakout、momentum、布林);策略參數 schema 化以支援 sweep。 | `analytics/backtest.py`(`Order`/`Context` 結構直接擴充)、`backtest_sweep_service.py` | `run_backtest` 簽名向下相容擴充;`/analytics/backtest` schema 加欄位 | M |
| C3 | 回測結果持久化與比較 | 回測 run 存檔(參數 + equity curve + metrics),可並排比較多次 run 與 walk-forward 結果對照。 | `models/backtest_sweep.py` 模式可仿、`strategy_comparison_service.py`、`walk_forward_service.py` | 新表 `backtest_runs`;`GET/POST /analytics/backtest-runs`;前端 `BacktestComparePanel.tsx` | M |
| C4 | 績效歸因(Brinson 簡化版) | 對投組報酬做「市場配置 vs 選股」拆解(TW/US/CRYPTO 三桶 + 個股貢獻度),以每日 snapshot 序列計算。 | `tasks/portfolio_snapshot.py`、`portfolio_analytics.py`、`analytics/risk.py` benchmark 邏輯 | `GET /portfolio/{id}/attribution`;前端 `AttributionPanel.tsx`;snapshot 表可能需加持倉明細欄位 | L |
| C5 | 再平衡建議 | 以 `portfolio_optimizer.py` 目標權重對比現況,產出最小交易清單(考慮手續費與整股限制),一鍵預覽不下單。 | `analytics/portfolio_optimizer.py`、`api/portfolio` `/optimise` 端點、FX 換算邏輯(`portfolio_service.py`) | `POST /portfolio/{id}/rebalance-plan`;前端 `RebalancePanel.tsx` | M |

依賴順序:C1 → C2 → C3 → C5 → C4(C4 依賴 snapshot 明細擴充,風險最高)。

## D. 告警與自動化

現況:`models/alert.py` 僅 above/below 單次觸發;`alert_service.py` 由三個 market refresh task 呼叫 `check_and_fire`;通知僅 WebSocket(`notification_service.py` 為可插拔 transport,設計良好)+ email 僅用於討論報告。

> ⚠️ **重要**:LINE Notify 已於 2025-03-31 終止服務,必須改用 **LINE Messaging API**(官方帳號 + channel access token;免費方案每月 200 則推播,超量付費 — 成本需列入評估)。

| # | 功能 | 說明 | 建構基礎 | 新增介面 | 規模 |
|---|------|------|----------|----------|------|
| D1 | 進階告警條件 | 條件型別擴充:漲跌幅 %、突破 N 日高/低、量能異常、RSI 穿越、外資連 N 日買超(TW)。改為規則引擎:`condition_type + params JSONB`,支援重複觸發與冷卻期。 | `alert_service.py` `check_and_fire`、`models/alert.py`、TW 籌碼資料表 | `price_alerts` 加 `condition_type/params/cooldown/repeat` 欄位(Alembic);`api/alerts` schema 擴充;前端 `AlertBuilder.tsx` | M |
| D2 | 通知通道:Email + LINE Messaging API | 通知從單一 WS 擴為多通道 fan-out:沿用 `notification_service.py` 的 registry 模式註冊 email(重用 `email_service.py` fail-closed 設計)與 LINE channel;使用者可設定每種告警走哪些通道。 | `notification_service.py`(改為 multi-transport)、`email_service.py`、`SettingsPage.tsx` | 新表 `notification_channels`(user_id, kind, config, verified);`/settings/notifications` API;LINE webhook 端點(綁定用)。**外部依賴:LINE Developers 官方帳號(免費額度有限)** | M |
| D3 | Web Push 瀏覽器推播 | 離線也能收到告警(service worker + VAPID),自架無外部付費服務,補足 WS 只在開頁時有效的缺口。 | `notification_service.py` registry、前端 PWA 已就緒 | 新表 `push_subscriptions`;`pywebpush` 依賴;前端 service worker + 訂閱 UI | M |
| D4 | 策略監控告警 | 把 `monitor_strategy_health.py` 與 `strategy_health_service.py` 的健康度劣化(Brier 上升、命中率跌破門檻)接進告警管線,主動通知而非只在頁面顯示。 | `tasks/monitor_strategy_health.py`、`strategy_health_service.py`、D1 規則引擎、D2 通道 | 告警規則加 `kind=strategy_health`;無新前端頁(AlertsPage 擴充) | S |
| D5 | 告警歷史與每日摘要 | 觸發紀錄留存(目前觸發後只改 flag)、AlertsPage 顯示歷史;可選每日收盤後 email 摘要(重用 auto-run email 排程模式)。 | `alert_service.py`、`tasks/scheduler.py` cron 模式、`email_service.py` | 新表 `alert_events`;`GET /alerts/history`;新 cron `daily_alert_digest` | S |

依賴順序:D1 → D5 → D2 → D3 → D4(D2/D3 可並行;D4 依賴 D1 引擎 + D2 通道)。

## 綜合優先矩陣

| 優先 | 功能 | 規模 | 價值/風險 | 理由 |
|------|------|------|-----------|------|
| P0 | A3 全螢幕、D5 告警歷史、D4 策略監控告警 | S | 高/低 | 純擴充、無 schema 風險,立即感受度高 |
| P0 | A1 技術指標、C1 風險儀表板 | M | 高/低 | 看盤與投組的核心缺口,資料與引擎皆已在庫 |
| P1 | D1 進階告警、B1 個股 AI 報告、A2 多週期 | M | 高/中 | D1 需 migration;A2 分時受 30 天保留限制需明示 |
| P1 | C2 回測擴充 → C3 持久化、B2 NL 查詢 | M | 高/中 | C2/C3 有先後依賴;B2 需嚴守唯讀白名單 |
| P2 | D2 LINE/Email 通道、D3 Web Push、A4 比較圖、C5 再平衡、B4 插話、B5 AI 投組健檢 | M | 中/中 | D2 涉外部服務(LINE 免費額度);B5 需越權防護設計 |
| P3 | B3 AI 選股助手、C4 績效歸因、A5 畫線工具 | L | 高/高 | 各自依賴前置基建(scoreboard 掛接、snapshot 明細、圖層重構或 v5 升級) |

**外部服務旗標**:
1. LINE Notify 已停役,改用 LINE Messaging API,免費 200 則/月後計費
2. TW 盤中分時無免費官方 API,以自有 `quote_snapshots`(60s)聚合為替代,僅近 30 天
3. Web Push 為自架 VAPID,無費用
4. 其餘功能皆不需新付費資料源

## 關鍵實作檔案

- `frontend/src/components/charts/CandlestickChart.tsx`(A 方向所有功能的宿主元件)
- `backend/services/alert_service.py` 與 `backend/models/alert.py`(D1/D4/D5 規則引擎改造核心)
- `backend/services/notification_service.py`(D2/D3 多通道 fan-out 的擴充點,registry 設計已就緒)
- `backend/analytics/backtest.py` 與 `backend/analytics/risk.py`(C1-C3 引擎)
- `backend/services/discussion/context_assembly.py` 與 `backend/ai/tools/financial.py`(B1/B2/B5 的 context 與 tool 基建)
