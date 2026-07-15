# 功能路線圖藍圖

> 本文件是[架構重規劃藍圖](00-overview.md)的原始功能章節。表格中的「新增介面」是
> 設計紀錄，不等同尚未開發；截至 2026-07-15，A1/A3/A4、B1/B2/B3/B4/B5、C1/C2/C3/C5、
> D1/D2/D3/D4/D5 與 A2/A5 已交付。實際路由、schema 與測試為權威狀態。

## A. 看盤與圖表進階

現況:`frontend/src/components/charts/CandlestickChart.tsx` 為單一 lightweight-charts v4 元件(K 線 + 量能疊加,無指標、無週期切換);`StockDetailPage.tsx` 以固定 360px 高度嵌入;資料僅有 `ohlcv_daily`(日 K)與 `quote_snapshots`(每 60 秒快照、30 天保留)。

| # | 功能 | 說明 | 建構基礎 | 新增介面 | 規模 |
|---|------|------|----------|----------|------|
| A1 | 技術指標疊加 | 在 K 線上疊加 MA/EMA/布林通道,副圖顯示 RSI/MACD/KD。指標於前端以純函式計算(日 K 資料量小),使用者可勾選開關並記憶偏好。 | `CandlestickChart.tsx`(`addLineSeries`、多 `priceScaleId` 副圖模式已有 vol 前例) | 新 `frontend/src/lib/indicators.ts`(純函式 + 單測)、`IndicatorToolbar.tsx`;偏好存 `localStorage`,無後端變更 | M |
| A2 | 多週期切換（日／週／月 + 分時，已交付） | 日→週／月由前端純函式聚合；TW/US/CRYPTO 以 `quote_snapshots` 聚合 1m/5m/15m OHLCV，正確處理累積成交量差分。無快照時回空集合並停用分時按鈕，UI 明示 30 天保留上限。 | `intraday_service.py`、`aggregateBars.ts`、`TimeframeSelector.tsx` | `GET /{market}/intraday/{symbol}`；跨 dialect Python 線性聚合與快取 | M |
| A3 | 全螢幕看盤模式 | K 線區一鍵放大至全視窗,支援 ESC 退出與高度自適應(現為固定 360px)。低風險高感受度。 | `CandlestickChart.tsx`(已有 ResizeObserver)、`StockDetailPage.tsx` | 前端 Fullscreen API wrapper + `height` 改為容器驅動;無後端 | S |
| A4 | 比較圖（已交付） | 選 2-5 檔標的，以所有成功 series 的共同可用起點 =100 正規化疊線，支援 TW/US/CRYPTO、1M/3M/6M/1Y、自選股一鍵帶入；同時揭露報酬、回撤、波動、來源與 degraded symbols。 | 各 market history service、`market_comparison_service.py`、`watchlist` | `GET /global/compare-history`;`ComparisonPage.tsx`;無新表 | M |
| A5 | 畫線工具（已交付） | K 線可建立水平支撐／壓力線與兩點式趨勢線，owner-scoped 保存並跨裝置同步；趨勢線支援端點重新定位與日曆時間動態投影，首次 tick 建立 relation 基準、真正換側才觸發 `trend_cross_above/below`。幾何更新同步告警並重建基準；水平線維持 `price_above/below`，`alert_id` 保證冪等。 | `CandlestickChart.tsx` price-line／line-series API、D1 evaluator registry、`ChartDrawing` | migrations 0081–0082；`/charts/drawings*`;`trend_alert_evaluations_total`;無額外圖表套件 | L |

依賴順序:A3 → A1 → A2 → A4 → A5(A5 依賴 A1 的圖層重構)。

### A6. 美股選擇權分析（已交付）

個股選擇權頁已從原始鏈表升級為 evidence-bounded 分析：Polygon/Massive 使用
`/v3/snapshot/options/{underlying}` 並展平 details、quote、trade、session、Greeks、IV、OI
與 underlying price；權限不足或失敗時回退 yfinance，且分析路徑只抓最近 8 個到期日，
避免全期限逐筆請求。`options-chain-analytics-v1` 計算 ATM call/put 平均 IV、期限結構、
年化 IV 預期波動、Put/Call OI/volume、最大痛點，以及明確命名的 90% put IV − 110% call IV
翼端偏斜（不冒充 25-delta skew）。缺少 spot／IV／OI 時對相依指標 abstain，API 同時回傳
coverage、flags、來源與方法說明。前端提供總覽、到期日合約鏈與 IV 曲面三視圖。

介面：`GET /api/us/options-analysis/{ticker}?max_expiries=1..12`；監控：
`options_analysis_total{outcome=good|degraded|unavailable}`。Polygon 頁面最多取 4×250 筆並
明示截斷旗標，避免無界 pagination。

## B. AI 能力強化

現況:`ai/llm_router.py`(8 供應商串流 + usage 記帳)、`ai/agents.py`(23 personas)、`ai/tools/`(quote/dcf/var/backtest/news/籌碼等 17 個 tool)、`services/discussion/`(圓桌 SSE、lessons、scoreboard、strategy template/sweep、auto-run 含 email 報告)。`api/ai_agents` 目前僅 `GET /agents` + `POST /chat`。

| # | 功能 | 說明 | 建構基礎 | 新增介面 | 規模 |
|---|------|------|----------|----------|------|
| B1 | 個股 AI 研究報告 | 從 StockDetailPage 一鍵產生單檔深度報告:估值(DCF)、籌碼、營收、新聞情緒、技術面,結構化輸出並可存檔/寄送。重用 discussion 的 context 組裝而非重寫。 | `services/discussion/context_assembly.py`、`technicals.py`、`ai/tools/financial.py`、`email_service.py` | `POST /ai/stock-report/{market}/{symbol}`(SSE);新表 `stock_reports`(快取 + 歷史);前端 `StockAIReportPanel.tsx` | M |
| B2 | 自然語言查詢（已交付） | Screener 輸入自然語言後，由 tool-capable persona 轉成一次 `run_screener` 呼叫；僅接受市場別白名單 filter，不執行模型 SQL，結果以既有表格呈現。外資連買使用固定 ORM SELECT。未支援指標會明示而不假造。 | `ai/tools/screener.py`、既有 TW/US screener、AI SSE tool_result | `NLQueryBar.tsx`;`run_screener`;無新表 | M |
| B3 | AI 研究候選助手（已交付） | 每日排程將近 7 天、品質 ≥70%、結論偏多的 evidence-backed 個股報告依品質與時效排名；保存來源報告、方法版本、證據與品質細節，並從次一交易日進入 D1/D5/D20 決策日誌。 | `stock_report_service.py` 的 citation/source-quality gate、`decision_journal_service.py` | `stock_pick_runs`;`/research/daily-picks*`;Research Workspace Picks tab；Prometheus/Grafana pipeline metrics | L |
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
| C4 | 績效歸因（第二階段已交付） | `modified-dietz-cash-ledger-v2` 將原生幣別現金建模為 sleeve，使買賣結算在證券／現金間抵銷，僅真正入出金調整分母，股息及現金 FX 留在報酬；拆解 CASH/TW/US/CRYPTO 與個股貢獻並對照 TAIEX TR／SPY。不冒充缺少 point-in-time 基準權重的 Brinson，完整 allocation/selection/interaction 留待基準成分歷史。 | `portfolio_attribution_service.py`、append-only 現金帳本、交易 ledger、歷史價格／FX | `GET /portfolio/{id}/attribution`;前端 `AttributionPanel.tsx`;使用 migration `0086` 帳本 | L |
| A7 | 台股三表深度分析（已交付） | 整合損益表、資產負債表與現金流量表，提供完整四季 TTM、DuPont ROE、現金含金量、同季 YoY、描述性風險訊號與欄位涵蓋率；缺季不外推、缺欄位不猜值。 | FinMind XBRL 三表、`tw_health_metrics.py` | `GET /api/tw/health/{symbol}`；前端「三表分析」；無新表 | M |
| A8 | 台股可解釋多因子排名（已交付） | 以時點估值快照與公司行動還原價建立價值、品質、126-21 動能、低波動、股息、流動性六因子；5/95 winsorization + 橫斷面 z-score，缺值至少 60% 權重才排名。滾動驗證以上市／下市日期重建當時股票池。v3 每日封存產業分類並提供產業中性 composite；小於 2 檔的產業排除，總覆蓋不足 60% 則拒絕冒稱中性。v4 加入停牌、漲跌停鎖死最多延後 5 交易日、成交金額參與率、部分成交保留現金與平方根市場衝擊成本。v5 以 TAIEX IR0001 含息指數為預設獨立基準，覆蓋不足才整體降級，並提供 bootstrap 95% 區間、t-stat、資訊比率、超額勝率及多空／波動情境摘要。v6 加入前瞻 Rank IC、五分位報酬、Q5−Q1、因子相關矩陣、Holm 校正與持有期／Top-N 敏感度。v7 的品質因子由營業利益率、ROA、現金流／資產與負債比反向分數組成，三表依法定期限保守延後可用，不再把期末日誤當公告日。v8 只用已成熟的前瞻 IC 標籤進行受限 walk-forward 權重學習，設有 12 期暖機、24 期視窗、可靠度收縮、50%–150% 權重界線與固定權重回退；另揭露權重漂移及 5／21／63 日因子 IC 衰減。v9 以有效日期商品主檔排除 ETF，回傳主檔覆蓋率與 fallback 品質旗標。治理層再將完整研究結果與參數持久化，按使用者／profile 建立 candidate、champion、retired 模型版本；升級同時要求絕對品質閘門與相對 champion 改善，正式權重實際供排名使用且不跨租戶。訊號診斷不冒充可成交報酬，所有還原價、生命週期、財報可用日、分類、中性化、可成交與基準覆蓋率均由 API 揭露。 | `fundamentals_snapshots`、`ohlcv_daily`（含 `_TAIEX_TR`）、`finmind.tw_income_statement`、`finmind.tw_balance_sheet`、`finmind.tw_cash_flow`、`finmind.tw_stock_price_adj`、`finmind.tw_stock_info`、`finmind.tw_delisting`、`finmind.tw_price_limit_daily`、`finmind.tw_suspended`、`tw_company_classification_snapshots`、`tw_security_master_versions`、`tw_factor_research_runs`、`tw_factor_model_versions`、`tw_factor_service.py` | 排名／驗證 API 加上 `POST/GET /api/tw/factor-research-runs`、`GET /api/tw/factor-models`、`POST /api/tw/factor-models/{id}/promote`；前端可登錄挑戰模型、檢視失敗閘門與升級正式版本；migrations `0084`、`0085`、`0087` | L |
| A9 | 台股因子投組建構（已交付） | 將 profile／champion 因子排名轉為不下單目標投組；以 252 期共變異數與 TAIEX 含息指數控制年化波動及追蹤誤差，另限制最低投入、單檔、產業、現況換手與最近 20 期 ADV 參與率容量。回傳現金、產業曝險、個股風險貢獻、流動性上限與逐項 binding diagnostics；無可行解時拒絕等權重 fallback。 | `tw_factor_portfolio_optimizer.py`、`tw_factor_portfolio_service.py`、`ohlcv_daily._TAIEX_TR`、還原價格 sidecar | `POST /api/tw/factor-portfolio`；前端「受限因子投組」控制與結果表；無新表 | L |
| A10 | 因子投組實際持倉再平衡（已交付） | 以 owner-scoped 投組的台股持倉與新增現金重算目標，支援新標的買入、整張／零股、最小交易額、手續費下限、股票／ETF 賣出稅、逐代號稅率覆寫、滑價與平方根 ADV 衝擊；回傳成本三情境、資金缺口、交易後現金／權重漂移及交易前後風險。非台股凍結，無可行目標固定零交易，僅預覽不寫入交易。 | `tw_factor_rebalance.py`、`tw_factor_rebalance_service.py`、使用者投組與 A9 optimizer | `POST /api/tw/factor-portfolio/rebalance-preview`；前端實際持倉預覽與成本／風險表；無新表 | M |
| A11 | 多幣別現金帳本與完整每日快照（已交付） | Append-only 現金帳本依原生結算幣別記錄入出金、費稅、股息與交易結算；交易修改／刪除採反向分錄，阻擋時點負庫存賣出。舊交易回填結算與最小必要推定期初資金。每日快照保存逐部位、現金、淨清算價值及缺價品質，舊快照標記僅有總額。因子再平衡直接使用實際 TWD 現金，外幣凍結、假設追加資金另列。 | `PortfolioCashEntry`、`PortfolioSnapshot`、`portfolio_cash_service.py`、`portfolio_snapshot.py` | `/portfolio/{id}/cash`、`cash-entries`、`snapshots`；前端「現金帳本」；migration `0086` | L |
| A12 | 台股有效日期商品主檔（已交付） | 將股票、一般／主動式／債券／多資產／期貨／槓桿／反向 ETF 分類、整張與零股單位、賣出稅率及法規來源版本化；2026 年底前非槓反債券 ETF 停徵採明確失效日。每日 symbol refresh 自動同步，缺資料才回退公開編碼規則並揭露品質；管理員覆寫必須帶操作者、原因與有效區間。因子母體與實際持倉再平衡共用同一解析器。 | `TwSecurityMasterVersion`、`tw_security_master_service.py`、TWSE ETF 編碼、財政部證交稅規則 | `/api/tw/security-master/{symbol}`、admin sync／override；台股詳情規則卡與因子覆蓋率；migration `0087` | M |
| A13 | Paper trading 成交前核心（第一階段已交付） | Portfolio-scoped 冪等送單，依市場原生幣別保留買入資金與費用、依現有持倉保留賣出庫存；訂單支援 pending、部分成交、完成、取消與限價保護。每次模擬 fill 原子寫入不可變成交紀錄、既有 Transaction、Holding 與 append-only 現金／費用帳本，不另造平行投組。 | `PaperOrder`、`PaperFill`、`paper_trading_service.py`、A11 現金帳本 | `/portfolio/{id}/paper-orders*`；migration `0088`。行情驅動撮合、交易時段／TIF 與券商回報列第二階段 | M |
| C5 | 再平衡建議 | 以 `portfolio_optimizer.py` 目標權重對比現況,產出最小交易清單(考慮手續費與整股限制),一鍵預覽不下單。 | `analytics/portfolio_optimizer.py`、`api/portfolio` `/optimise` 端點、FX 換算邏輯(`portfolio_service.py`) | `POST /portfolio/{id}/rebalance-plan`;前端 `RebalancePanel.tsx` | M |

依賴順序:C1 → C2 → C3 → C5 → C4 完整 Brinson(第一階段 Modified Dietz 已不依賴 snapshot 明細)。

## D. 告警與自動化

現況:`models/alert.py` 僅 above/below 單次觸發;`alert_service.py` 由三個 market refresh task 呼叫 `check_and_fire`;通知僅 WebSocket(`notification_service.py` 為可插拔 transport,設計良好)+ email 僅用於討論報告。

> ⚠️ **重要**:LINE Notify 已於 2025-03-31 終止服務,必須改用 **LINE Messaging API**(官方帳號 + channel access token;免費方案每月 200 則推播,超量付費 — 成本需列入評估)。

| # | 功能 | 說明 | 建構基礎 | 新增介面 | 規模 |
|---|------|------|----------|----------|------|
| D1 | 進階告警條件（已交付） | 支援漲跌幅、N 日高低突破、量能異常、外資連買，以及 RSI(period, level) 真正向上／向下穿越。RSI 使用足量封存收盤加即時價，首次 tick 建基準、換側才觸發、資料不足 abstain；規則引擎以 `condition_type + params` 支援重複／冷卻，runtime state 經 migration 0082 持久化。 | `alert_service.py` evaluator registry、`ohlcv_daily`、TW 籌碼資料表 | `api/alerts`;`AlertBuilder.tsx`;`indicator_alert_evaluations_total` | M |
| D2 | 通知通道：Email + LINE Messaging API（已交付） | registry fan-out 已加入 opt-in Email／LINE transports；每位使用者可分別選擇價格與策略健康告警。Email 永遠解析目前帳號信箱；LINE 以 15 分鐘一次性代碼及 HMAC-SHA256 webhook 綁定，access token／secret 僅存在部署環境。provider 未設定時 fail-closed，連續失敗 5 次自動停用，並可在 Settings 傳送測試。 | `channel_notification_service.py`、`notification_service.py`、`NotificationChannels.tsx` | migration 0083；`/notifications/channels*`；`/notifications/line/webhook`；`notification_deliveries_total`。**啟用 LINE 仍需 LINE Developers 官方帳號** | M |
| D3 | Web Push 瀏覽器推播 | 離線也能收到告警(service worker + VAPID),自架無外部付費服務,補足 WS 只在開頁時有效的缺口。 | `notification_service.py` registry、前端 PWA 已就緒 | 新表 `push_subscriptions`;`pywebpush` 依賴;前端 service worker + 訂閱 UI | M |
| D4 | 策略監控告警 | 把 `monitor_strategy_health.py` 與 `strategy_health_service.py` 的健康度劣化(Brier 上升、命中率跌破門檻)接進告警管線,主動通知而非只在頁面顯示。 | `tasks/monitor_strategy_health.py`、`strategy_health_service.py`、D1 規則引擎、D2 通道 | 告警規則加 `kind=strategy_health`;無新前端頁(AlertsPage 擴充) | S |
| D5 | 告警歷史與每日摘要（已交付） | 觸發紀錄以 owner-scoped append-only event 保存並顯示於 AlertsPage；每日 Email 摘要預設關閉，只有使用者在 Email 通道明確 opt-in 才聚合最近 24 小時事件，避免未同意郵件及與即時 Email 重複。 | `alert_service.py`、`daily_alert_digest.py`、D2 Email 偏好 | `alert_events`;`GET /alerts/history`;`daily_alert_digest` cron | S |

依賴順序:D1 → D5 → D2 → D3 → D4(D2/D3 可並行;D4 依賴 D1 引擎 + D2 通道)。

## 綜合優先矩陣

| 優先 | 功能 | 規模 | 價值/風險 | 理由 |
|------|------|------|-----------|------|
| P0 | A3 全螢幕、D5 告警歷史、D4 策略監控告警 | S | 高/低 | 純擴充、無 schema 風險,立即感受度高 |
| P0 | A1 技術指標、C1 風險儀表板 | M | 高/低 | 看盤與投組的核心缺口,資料與引擎皆已在庫 |
| P1 | D1 進階告警、B1 個股 AI 報告、A2 多週期 | M | 高/中 | D1 需 migration;A2 分時受 30 天保留限制需明示 |
| P1 | C2 回測擴充 → C3 持久化、B2 NL 查詢 | M | 高/中 | C2/C3 有先後依賴;B2 需嚴守唯讀白名單 |
| P2 | D2 LINE/Email 通道、D3 Web Push、A4 比較圖、C5 再平衡、B4 插話、B5 AI 投組健檢 | M | 中/中 | D2 涉外部服務(LINE 免費額度);B5 需越權防護設計 |
| P3 | B3 AI 選股助手、C4 績效歸因、A5 畫線工具（皆已交付第一個可用版本） | L | 高/高 | 已具方法版本、歸因明細、持久化圖層與動態穿越告警；完整 Brinson 仍需 point-in-time 基準資料 |

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
