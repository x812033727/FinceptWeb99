# Fincept Web Terminal

<div align="center">

[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-C06524)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18+-61DAFB?logo=react&logoColor=black)](https://react.dev/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)

### **Your Thinking is the Only Limit. The Data Isn't.**

專業級金融智能平台——伺服器版，支援 CFA 級分析、AI 自動化，以及美股、台股與全球市場數據整合。

> **Professional Beta 定位：**台股優先、美股支援、僅提供決策支援；目前採管理員邀請制，
> 不提供真實券商下單、資金託管或投資建議。

[🚀 快速啟動](#快速啟動) · [💬 討論](https://github.com/x812033727/finceptweb/discussions) · [🐛 回報問題](https://github.com/x812033727/finceptweb/issues)

</div>

---

## 關於本專案

**Fincept Web Terminal** 是 [Fincept Terminal](https://github.com/Fincept-Corporation/FinceptTerminal) 的**伺服器版本**，將原本的 C++/Qt6 桌面應用重新設計為以 **FastAPI（後端）+ React（前端）** 為核心的 Web 服務架構。

- 無需在客戶端安裝任何軟體，透過瀏覽器即可存取所有功能
- 支援多人同時使用，適合團隊、機構及教育環境部署
- 針對**美股（US Equities）**與**台股（TWSE / TPEx）**市場深度整合，同時保留全球市場覆蓋

---

## 架構概覽

```
FinceptWeb/
├── backend/              # FastAPI 伺服器
│   ├── api/              # REST API 路由（一域一套件）
│   │   ├── auth/         # 邀請啟用、JWT 登入／刷新／登出、session、API 金鑰
│   │   ├── us_market/    # 美股報價、歷史、基本面、選擇權、總經、新聞
│   │   ├── tw_market/    # 台股行情、歷史、法人、融資券、月營收、新聞
│   │   ├── crypto_market/ # Kraken-backed crypto 報價、歷史、篩選與搜尋
│   │   ├── portfolio/    # 持倉、交易紀錄、P&L、最佳化
│   │   ├── theses/       # 投資論點、review 與事件時間線
│   │   ├── decision_journal/ # D1／D5／D20 決策結果
│   │   ├── research/     # 每週研究與投組摘要
│   │   ├── analytics/    # DCF、VaR、策略回測
│   │   ├── ai_agents/    # SSE 串流對話（19 人格、8 LLM 供應商）
│   │   ├── discussion/   # 多專家圓桌討論、背景回合、結論與 auto-run
│   │   ├── watchlist/    # 多觀察清單 CRUD + 即時報價擴充
│   │   ├── alerts/       # 價格警報 CRUD
│   │   ├── system/       # 版本檢查、Core Web Vitals 回報
│   │   └── websocket/    # Auth-first WS、Redis Pub/Sub、delta 抑制
│   ├── ai/               # LLM 路由器 + 代理人人格定義
│   ├── analytics/        # 純計算：dcf.py、risk.py、backtest.py
│   ├── auth/             # JWT handler + 角色權限
│   ├── cache/            # Redis helpers（get/set/delete、key helpers）
│   ├── data/
│   │   ├── us/           # Polygon → yfinance → Stooq → Finnhub；FRED
│   │   ├── tw/           # TWSE → FinMind → MOPS 瀑布
│   │   └── crypto/       # Kraken REST / WebSocket 與 Top 20 universe
│   ├── db/               # Alembic 0001-0087、引擎、seed
│   ├── middleware/        # Prometheus metrics 中介層
│   ├── models/           # SQLAlchemy ORM：User、Portfolio、Holding 等
│   ├── services/         # 業務邏輯（快取、瀑布、LLM routing）
│   ├── tasks/            # APScheduler：行情、ingest、討論 auto-run、驗證
│   └── tests/            # pytest（in-memory SQLite + AsyncMock Redis）
├── frontend/             # React 18 + TypeScript + Vite
│   ├── public/           # PWA：manifest、service worker、icon
│   └── src/
│       ├── components/
│       │   ├── charts/   # CandlestickChart（lightweight-charts v4）
│       │   ├── layout/   # AppLayout、Sidebar、NotificationBell
│       │   └── portfolio/ # AllocationPie、HoldingsTable
│       ├── hooks/        # useWebSocket、usePortfolio
│       ├── pages/        # 每個路由一個檔案（共 14 頁）
│       ├── store/        # Zustand：authStore、notificationStore、themeStore、toastStore
│       ├── types/        # TypeScript 介面
│       └── lib/          # api.ts（axios）、auth.ts（silentRefresh）
├── helm/fincept-web/     # Kubernetes Helm chart
├── docker/               # nginx.conf、redis.conf
├── .github/workflows/    # CI（pytest + lint + build）、Docker GHCR 推送
└── docker-compose.yml
```

---

## 功能特色

| 功能 | 說明 |
|------|------|
| 📊 **CFA 級分析** | DCF 估值模型、投資組合最佳化（均值-變異數）、VaR（歷史／參數／Monte Carlo）、策略回測 |
| 🇺🇸 **美股整合** | 即時報價、歷史 K 線、基本面、選擇權鏈、選股篩選器、S&P 500 搜尋、盈餘日期 |
| 🇹🇼 **台股整合** | 上市櫃即時行情、法人買賣超、融資融券、月營收、財報（MOPS）、K 線圖 |
| 🪙 **加密貨幣整合** | Kraken REST + WebSocket，Top 20 crypto 報價、歷史、篩選與搜尋 |
| 🤖 **AI 代理人與討論** | 19 位人格、8 個 LLM provider、SSE 串流聊天、多專家圓桌討論、每日 per-user auto-run 與 5 日後自動驗證 |
| 📈 **即時串流** | WebSocket（Auth-first）、Redis Pub/Sub 扇出、delta 抑制、30s 心跳；crypto 由 Kraken pump 推送 |
| 🔬 **量化計算** | DCF 敏感度 5×3 網格、VaR Cholesky 相關、事件驅動回測（SMA crossover、RSI）|
| 👥 **多用戶管理** | JWT（15min access + 7d refresh）、API 金鑰、三級角色（viewer / analyst / admin）|
| 🐳 **容器化部署** | Docker Compose 一鍵啟動，Kubernetes Helm chart 水平擴展 |

---

## 市場覆蓋

### 🇺🇸 美股（US Equities）

| 類別 | 內容 |
|------|------|
| **交易所** | NYSE、NASDAQ、AMEX、OTC |
| **指數** | S&P 500、NASDAQ 100、Dow Jones、Russell 2000 |
| **資料類型** | 即時報價、歷史 OHLCV、基本面、選擇權鏈、盈餘預測 |
| **總體經濟** | FRED（Fed Funds Rate、CPI、GDP、殖利率曲線、USD 指數、TWD/USD）|
| **資料來源** | Polygon.io → yfinance 瀑布、FRED |

### 🇹🇼 台股（Taiwan Equities）

| 類別 | 內容 |
|------|------|
| **交易所** | 臺灣證券交易所（TWSE）、證券櫃檯買賣中心（TPEx）|
| **指數** | 加權股價指數（TAIEX）、OTC 指數 |
| **資料類型** | 即時行情、歷史 OHLCV、每日成交統計 |
| **籌碼分析** | 外資（FINI）、投信、自營商買賣超；融資融券餘額 |
| **財報資料** | 月營收、基本面（PE／PB／殖利率）|
| **資料來源** | TWSE OpenAPI → FinMind → 公開資訊觀測站（MOPS）瀑布 |

---

## 快速啟動

### 方式一：Docker Compose（推薦）

```bash
git clone https://github.com/x812033727/finceptweb.git
cd finceptweb

# 複製並設定環境變數
cp backend/.env.example backend/.env
# 在 .env 中填入您的 API 金鑰（見下方說明）

# 啟動所有服務
docker compose up -d
```

服務啟動後：
- **前端**：http://localhost
- **API 文件**：http://localhost/docs
- **API（ReDoc）**：http://localhost/redoc

> **生產部署提示：** 設定強密碼於 `.env`（`POSTGRES_PASSWORD`、`JWT_SECRET_KEY`、`REDIS_PASSWORD`），
> 並在 nginx 前端掛載 TLS 憑證（Certbot / Let's Encrypt）。

---

### 方式二：本地開發環境

#### 前置需求

| 工具 | 版本 |
|------|------|
| Python | 3.11+ |
| Node.js | 20+ |
| PostgreSQL | 15+ |
| Redis | 7+ |

#### 後端

```bash
cd backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt

# 複製環境變數
cp .env.example .env               # 填入 API 金鑰

# 資料庫初始化
alembic upgrade head

# 啟動 API 伺服器
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

#### 前端

```bash
cd frontend
npm install
npm run dev                        # Vite dev server → http://localhost:5173
```

#### 執行測試

```bash
cd backend
pytest tests/ -v --asyncio-mode=auto
```

測試使用 in-memory SQLite（aiosqlite）+ AsyncMock Redis，無需外部服務。

---

## 環境變數設定

`backend/.env`：

```env
# 資料庫
DATABASE_URL=postgresql+asyncpg://fincept:password@localhost:5432/finceptweb
REDIS_URL=redis://localhost:6379

# 安全性
JWT_SECRET_KEY=<min-32-char-secret>
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=<password>

# 美股資料（Polygon 為選用，fallback 至 yfinance）
POLYGON_API_KEY=

# 台股資料（FinMind 為選用）
FINMIND_TOKEN=

# 總體經濟（FRED 為選用）
FRED_API_KEY=

# AI / LLM（依需求填入）
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
OLLAMA_HOST=http://localhost:11434

# 其他
DEBUG=false
CORS_ORIGINS=http://localhost:5173
```

---

## API 端點概覽

### 認證

```
POST /api/admin/invitations          # 管理員建立單次、限時、綁定 email 的邀請
POST /api/auth/accept-invite         # 接受邀請並建立帳號
POST /api/auth/login                 # 登入（回傳 access token）
POST /api/auth/refresh               # 刷新 access token（httpOnly cookie）
POST /api/auth/logout                # 登出（撤銷 refresh token）
POST /api/auth/password/forgot       # 忘記密碼（防帳號列舉）
POST /api/auth/password/reset        # 單次 token 重設密碼
GET  /api/auth/sessions              # 列出目前工作階段
DELETE /api/auth/sessions/{id}       # 撤銷指定工作階段
GET  /api/auth/me                    # 取得目前使用者
PATCH /api/auth/me                   # 更新個人資料
GET  /api/auth/api-keys              # 列出 API 金鑰
POST /api/auth/api-keys              # 建立 API 金鑰
DELETE /api/auth/api-keys/{id}       # 刪除 API 金鑰
```

公開註冊在正式環境預設關閉；`POST /api/auth/register` 僅供明確啟用的本機／測試環境相容使用。

### 專業研究工作流

```
GET/POST /api/theses                         # 使用者專屬 thesis 列表／建立
GET/PATCH/DELETE /api/theses/{id}            # owner-scoped thesis CRUD
POST /api/theses/{id}/review                 # 保存論點 review
GET  /api/theses/{id}/timeline               # 公告、新聞、營收、法人、告警時間線
POST /api/portfolio/{id}/stress-test         # TAIEX／半導體／匯率／利率／跳空情境
GET  /api/portfolio/{id}/attribution         # Modified Dietz 資金流調整績效與持倉／市場貢獻
GET  /api/decision-journal                    # D1／D5／D20 成本後結果與校準
GET  /api/research/weekly-summary             # 每週研究、風險與待處理摘要
POST /api/research/daily-picks/generate       # 由可信 AI 報告產生每日 TW／US 候選排名
GET  /api/research/daily-picks/latest         # 各市場最新候選與 evidence／品質資訊
GET  /api/research/daily-picks                # 候選 run 歷史
GET  /api/global/compare-history              # 2–5 檔 TW／US／CRYPTO 共同基準相對績效
GET  /api/charts/drawings/{market}/{symbol}   # 使用者專屬標的畫線
POST /api/charts/drawings                     # 建立水平線／趨勢線資料
PATCH/DELETE /api/charts/drawings/{id}        # 畫線更新／刪除
POST /api/charts/drawings/{id}/alert          # 水平線／趨勢線冪等轉換成動態告警
POST /api/feedback/data-quality               # 使用者資料品質回報
GET  /api/admin/beta/metrics                  # Beta 啟用、活躍與流程指標（admin）
```

TW Thesis 的 `watch_conditions` 支援可執行條件（營收 YoY／MoM、外資淨流量與
`lt/lte/gt/gte/eq` 比較）。排程器會用已封存資料自動評估，命中後寫入可追溯、
冪等的 `watch_condition_triggered` timeline event，並在每週研究摘要列為待處理項目；
舊版自由文字條件仍保持相容，但不會被自動執行。

市場資料 response 以 additive `meta` 回傳 `source`、`as_of`、`market_session`、
`freshness`、`fallback_chain`、`consistency`、跨來源價差及品質旗標。個股詳情頁會以
`verify=true` 額外查詢一個獨立來源；若價差超過 US 1%／TW 收盤資料 0.5%，介面顯示
來源衝突警告，Prometheus 也會以 bounded provider labels 記錄結果。台股盤中不拿
EOD FinMind 與 MIS 即時價硬比，而是明確標記 `unverified`，避免製造假警報。
AI 個股報告保存模型、prompt 版本、資料截止時間、來源快照與 evidence；生成前會
主動交叉驗證 quote，衝突數值會從 LLM context 移除。引用完整度仍必須為 100%
才能保存，而報告 `quality_score` 另乘上來源可靠度（衝突／過期／無法驗證／connector
錯誤皆會降分），API 與前端會揭露 reliability band 與問題數量。

每日候選只採用近 7 天、`quality_score >= 70%` 且結論明確偏多的最新報告；以
報告品質、時效與 stance 組成可重現分數，保存 `trusted-report-ranking-v1` 方法版本、
來源報告與 evidence。每個候選會自動寫入 `ai_stock_pick` 決策日誌，從下一個可用
交易日開盤起計算成本後 D1／D5／D20 報酬，避免 look-ahead bias。

投組績效歸因已升級為 `modified-dietz-cash-ledger-v2`，將每個原生幣別現金視為獨立
sleeve；買賣結算是現金與證券間的內部流動，只有真正入金／出金調整投組分母，股息與
現金匯率損益保留為投資報酬。結果拆成 CASH／TW／US／CRYPTO 市場桶與個股貢獻，
再對照 TWD 投組的 TAIEX TR 或其他投組的 SPY。由於目前沒有 point-in-time 基準成分與產業權重，介面
會明確標示這不是 Brinson 產業配置／選股歸因，避免對使用者呈現偽精確結果。

跨市場比較頁可由自選股一鍵帶入最多 5 檔，或用全市場搜尋加入台股、美股與加密貨幣；
後端以各標的共同可用起點正規化為 100，並回傳區間報酬、最大回撤、年化波動、來源
與資料不足清單。跨幣別結果維持各自原生報價幣別，不暗中混入不透明的即期匯率假設。

個股 K 線支援點擊建立水平支撐／壓力線，以及以兩次點擊建立趨勢線；線段以
owner-scoped `chart_drawings` 保存並跨裝置同步。趨勢線可用端點重新定位模式更新，
適用滑鼠與觸控操作；每條水平線可依相對現價一鍵建立 `price_above`／`price_below`
告警。趨勢線則依日曆時間插值／外推目標價，現價在線下時建立向上穿越告警、在線上
時建立向下穿越告警；首次 tick 只保存 relation 基準，後續真正換側才觸發。端點更新
會同步告警幾何並重建基準，`alert_id` 保證重複點擊不會建立多筆規則。評估結果以
`trend_alert_evaluations_total{outcome}` 監控，runtime state 由 migration 0082 持久化。

進階告警亦支援 RSI 向上／向下穿越：以最多 252 筆已封存日收盤加當前即時價做
Wilder 平滑計算（至少需 `period` 筆歷史），首次 tick 只建立門檻上下側基準，真正換側才觸發；資料不足時明確 abstain，
不以短序列猜值。建立、重複／冷卻、告警歷史、WebSocket／Web Push 與 thesis timeline
皆沿用同一規則引擎，並以 `indicator_alert_evaluations_total{indicator,outcome}` 監控。

告警可依使用者偏好 fan-out 至 WebSocket、Web Push、Email 與 LINE Messaging API。
Email 與 LINE 可分別訂閱價格／策略健康事件並傳送測試；LINE 綁定使用 15 分鐘一次性
代碼，webhook 必須通過 channel secret 的 HMAC-SHA256 簽章，provider token 不進資料庫。
連續 5 次傳送失敗會自動停用該通道，送達／失敗／篩選／未設定狀態以
`notification_deliveries_total{channel,outcome}` 監控。正式啟用 LINE 前，需將 Developers
Console webhook 設為 `/api/notifications/line/webhook`。
每日 Email 告警摘要獨立採明確 opt-in，預設不寄送；停用即時 Email 時仍可只保留每日摘要。

```text
GET  /api/notifications/channels                # Email／LINE 狀態與事件偏好
PUT  /api/notifications/channels/{email|line}   # 啟停與事件類型
DELETE /api/notifications/channels/{email|line} # 移除設定／解除 LINE 綁定
POST /api/notifications/channels/line/bind      # 建立一次性 LINE 綁定碼
POST /api/notifications/channels/{kind}/test    # 傳送測試通知
POST /api/notifications/line/webhook             # LINE 簽章 webhook
```

### 美股

```
GET  /api/us/quote/{ticker}          # 即時報價
GET  /api/us/history/{ticker}        # 歷史 K 線（?period=1y&interval=1d）
GET  /api/us/fundamentals/{ticker}   # 基本面資料
GET  /api/us/options/{ticker}        # 選擇權鏈
GET  /api/us/options-analysis/{ticker} # ATM IV、期限結構、偏斜、P/C、最大痛點
GET  /api/us/screener                # 選股篩選器（?sector=&min_market_cap=&max_pe=）
GET  /api/us/news/{ticker}           # 個股新聞
GET  /api/us/search                  # 代號搜尋（?q=AAPL）
GET  /api/us/earnings/{ticker}       # 盈餘日期與預估
GET  /api/us/macro                   # 總體經濟指標（FRED）
```

### 台股

```
GET  /api/tw/quote/{symbol}          # 即時行情（e.g., 2330）
GET  /api/tw/history/{symbol}        # 歷史 K 線
GET  /api/tw/fundamentals/{symbol}   # 基本面（PE／PB／殖利率）
GET  /api/tw/institutional/{symbol}  # 法人買賣超
GET  /api/tw/margin/{symbol}         # 融資融券
GET  /api/tw/revenue/{symbol}        # 月營收
GET  /api/tw/screener                # 選股篩選器
GET  /api/tw/factor-ranking          # 可解釋六因子 point-in-time 排名
GET  /api/tw/factor-validation       # 含換手與成本的滾動前瞻驗證
POST /api/tw/factor-portfolio        # 受限因子目標投組（不下單）
POST /api/tw/factor-portfolio/rebalance-preview # 依名下實際持倉產生交易預覽
GET  /api/tw/security-master/{symbol} # 有效日期商品分類、整張單位與賣出稅率
POST /api/tw/security-master/sync     # 管理員同步商品主檔
PUT  /api/tw/security-master/{symbol}/override # 管理員帶原因人工覆寫
GET  /api/tw/news/{symbol}           # 個股新聞
GET  /api/tw/indices                 # 大盤指數
```

多因子動能需要至少 148 個交易日。新部署可在 `backend/` 執行下列可續跑回填；
已完整歸檔的月份會自動跳過，並可用 `--start-after` 或 `--max-symbols` 分批執行：

```bash
python -m scripts.backfill_ohlcv_tw --months 8
```

排名與驗證會優先採用除權息／分割後的還原收盤價，並以 FinMind 股票主檔與下市日
重建歷史股票池。先初始化 FinMind schema，再執行可續跑的研究資料回填：

```bash
python -m finmind.scripts.init_db
python -m finmind.scripts.backfill --dataset TaiwanStockInfo --days 30
python -m finmind.scripts.backfill --dataset TaiwanStockDelisting --days 3650
python -m finmind.scripts.backfill --dataset TaiwanStockPriceAdj --days 550 \
  --universe-from-tw-stock-info --include-delisted
python -m finmind.scripts.backfill --dataset TaiwanStockPriceLimit --days 550
python -m finmind.scripts.backfill --dataset TaiwanStockSuspended --days 3650
python -m finmind.scripts.backfill --dataset TaiwanStockFinancialStatements --days 2200 \
  --universe-from-tw-stock-info --include-delisted
python -m finmind.scripts.backfill --dataset TaiwanStockBalanceSheet --days 2200 \
  --universe-from-tw-stock-info --include-delisted
python -m finmind.scripts.backfill --dataset TaiwanStockCashFlowsStatement --days 2200 \
  --universe-from-tw-stock-info --include-delisted
```

API 的 `quality.adjusted_price_coverage_pct` 與 `point_in_time_universe` 會揭露
回填完整度；未完成時自動降級並保留偏誤警示，不會把原始價格誤稱為還原價。

自 migration `0084` 起，每次台股公司主檔 refresh 也會將當日名稱、交易所與產業
寫入 `tw_company_classification_snapshots`。多因子 v3 預設啟用產業中性排名：先算
五因子 composite，再扣除同產業平均並重新標準化；每個產業至少需要 2 檔合格股票，
總產業覆蓋至少需 60%。API 會回傳 `classification_coverage_pct`、
`sector_coverage_pct` 與 `sector_neutral_applied`。歷史快照只從部署 `0084` 後開始累積，
不會用今天的分類回填過去日期；舊期間會保留 `sector_classification_not_point_in_time`
警示。

多因子 v4 的滾動驗證再加入可交易性模擬：遇停牌、漲停無法買進或跌停無法賣出時，
最多延後 5 個市場交易日；每檔目標部位受「當日成交金額 × 最大參與率」限制，未成交
資金保留為現金，市場衝擊採 `impact_coefficient_bps × √participation_rate` 的買賣雙邊
模型。驗證 API 可設定 `portfolio_notional_twd`、`max_participation_rate` 與
`impact_coefficient_bps`，並回傳平均成交率、容量受限、延遲與無法成交筆數。若缺少
漲跌停或停牌歷史，結果自動降級，不會以無摩擦報酬冒充可成交績效。

多因子 v5 預設以每日排程寫入 `ohlcv_daily._TAIEX_TR` 的 IR0001 臺灣加權含息報酬
指數作獨立基準；可用前瞻期間覆蓋不足 80% 時，整份報告才一致降級為可排名母體等權重，
不在同一報告混用基準。驗證另回傳平均超額報酬的 t-stat、固定種子的 2,000 次 percentile
bootstrap 95% 區間、年化資訊比率、超額勝率，以及依基準報酬與錨點前 63 交易日波動
劃分的多頭／空頭、高波動／低波動摘要。API 可用 `benchmark=taiex_total_return|equal_weight`
明確選擇，`quality.benchmark_coverage_pct`、`benchmark_used` 與統計樣本警示會同步揭露。

多因子 v6 進一步將「投組可成交績效」與「因子訊號診斷」分開。每個歷史錨點會以
當時可見的 composite 與五個因子分數，對下一交易日起算的公司行動還原價遠期報酬
計算 Spearman Rank IC、五分位平均報酬與 Q5−Q1 差，並彙整因子排名相關矩陣。
六個 IC 訊號的 p-value 以 Holm 法做報告內多重檢定校正；API 也回傳 5／21／63
交易日持有期與 Top 10／20／50 廣度敏感度。上述 IC、分位數與敏感度均是不含成本的
訊號診斷，不冒充可成交績效；校正範圍亦不包含使用者重複呼叫 API 或未揭露的試驗。

多因子 v7 新增「品質」因子，由營業利益率、ROA、營業現金流／資產與負債比反向
分數組成，至少兩個子指標才計分。FinMind/MOPS 三表的 `date` 是報表期末而非公告日，
因此歷史研究不再直接用 `date <= as_of`：Q1–Q3 保守延後至期末後第 46 天，年度報告
延後至翌年 4 月 1 日才可使用，並在 API 揭露公告時點為法定期限估算。品質加入後，
Holm 校正涵蓋 composite 加六因子共七個訊號；原始公告時間、延長申報或遲交仍是
明確限制，不會被隱藏。

多因子 v9 提供 `weight_mode=walk_forward|fixed`。受限滾動學習只會使用持有期已在目前
錨點前結束的 Rank IC 標籤，至少 12 期才啟用、最多使用最近 24 期，並以可靠度收縮將
每個因子限制在設定檔基準權重的 50%–150%；暖機或涵蓋不足時明確回退固定權重，避免
用未成熟標籤偷看未來。API 與前端會揭露自適應／回退期數、權重換手與各因子最新／
最小／最大權重，並比較 5／21／63 交易日 Rank IC 衰減，方便辨識訊號有效期與不穩定漂移。

模型治理層會將 `POST /api/tw/factor-research-runs` 的完整驗證參數、結果、門檻判定與
候選權重持久化。每位使用者、每個 profile 各自維護 candidate／champion／retired
版本；至少 24 個完整期間、12 個自適應期間、composite IC ≥ 0.03 且通過 Holm、平均
超額報酬非負、超額勝率 ≥ 55%、最大回撤不低於 -20%、平均成交率 ≥ 80%、最大權重
換手 ≤ 10%，並使用 TAIEX 含息基準才具升級資格。已有 champion 時，挑戰者還須增加
至少 0.10 個百分點平均超額報酬，且 IC 不得衰退超過 0.01。通過後可明確手動或選擇
自動升級；生存者偏誤、未還原價格、非時點分類、缺少停牌／漲跌停歷史或低診斷覆蓋
任一存在時也會拒絕升級。正式模型會實際供該使用者的即時排名使用，其他使用者不受影響。

`POST /api/tw/factor-portfolio` 會把目前 profile 或 champion 排名轉為不自動下單的目標
投組。SLSQP 求解同時限制最低投入、單檔／產業上限、252 期年化波動、相對 TAIEX
含息指數追蹤誤差、現況換手預算，以及依最近 20 期平均成交金額與參與率推導的個股
容量；未配置部分明確保留現金。API 逐項回傳實際值、門檻、是否通過與是否成為 binding
constraint，並揭露產業曝險、風險貢獻、流動性上限與排除原因。限制無可行解時不以
等權重偽裝成功，而是回傳 `converged=false` 與空部位。

`POST /api/tw/factor-portfolio/rebalance-preview` 會先以登入使用者名下投組的台股持倉與
新增現金推導可投資金額，再重新求解目標，而非要求前端手動抄權重。結果包含買賣股數、
整張／零股取整、交易後現金與權重漂移、交易前後波動／追蹤誤差、手續費、賣出證交稅、
滑價、平方根 ADV 市場衝擊，以及低／基準／壓力成本情境；非台股部位維持凍結。最佳化
無可行解時固定回傳零交易，空目標永遠不會被解讀為全部賣出。此 API 只讀且不建立交易。
由於目前尚未保存歷史持倉與現金快照，實際持倉預覽只接受今日日期，拒絕把今日部位與
歷史因子價格混算；缺少 ADV 的既有部位則保守套用設定的最大市場衝擊並回傳品質旗標。

Migration `0087` 新增有效日期商品主檔。每日 TWSE／TPEx 代號更新後會同步股票、股票型
ETF、債券 ETF、主動式、多資產、期貨、槓桿與反向型分類，以及整張／零股單位和賣出
稅率；每筆規則保留來源、生效／失效日與信心狀態。管理員可用帶原因、操作者及日期區間
的人工覆寫修正特殊商品。再平衡以主檔為準，僅在缺資料時回退公開代號規則並回傳品質
旗標；請求層 `sell_tax_bps_by_symbol` 則保留作明確情境分析。台股詳情會顯示目前商品規則
及官方依據，因子排名也揭露商品主檔覆蓋率。

現行股票賣出稅率為 30 bps、ETF 為 10 bps；非槓桿／反向債券 ETF 至 2026-12-31
停徵，規則以有效日期保存，到期後不會繼續套用 0 稅率。參考：
[證券交易稅條例](https://law-out.mof.gov.tw/LawContent.aspx?id=FL006079)、
[財政部停徵說明](https://www.etax.nat.gov.tw/etwmain/tax-info/understanding/tax-q-and-a/national/securities-transaction-tax/taxation-scope/7r3MjNB)、
[TWSE ETF 編碼](https://accessibility.twse.com.tw/downloads/zh/ETF/ETFcode.pdf)。

### 加密貨幣

```
GET  /api/crypto/quote/{symbol}      # 即時報價（e.g., BTC）
GET  /api/crypto/history/{symbol}    # 歷史 K 線
GET  /api/crypto/screener            # Top 20 crypto 篩選器
GET  /api/crypto/search              # 代號搜尋
GET  /api/crypto/news/{symbol}       # 相關新聞
```

### 投資組合

```
GET  /api/portfolio                  # 列出所有投資組合
POST /api/portfolio                  # 建立投資組合
GET  /api/portfolio/{id}             # 投資組合詳情（持倉、P&L）
DELETE /api/portfolio/{id}           # 刪除投資組合
POST /api/portfolio/{id}/transaction # 新增交易（buy / sell）
GET  /api/portfolio/{id}/cash        # 多幣別現金餘額與基準幣別換算
GET  /api/portfolio/{id}/cash-entries # append-only 現金帳本
POST /api/portfolio/{id}/cash-entries # 入金／出金／費用／稅款等現金異動
POST /api/portfolio/{id}/cash-entries/{entry_id}/reverse # 以反向分錄沖銷
GET  /api/portfolio/{id}/snapshots   # 每日持倉、現金與估值品質快照
GET  /api/portfolio/{id}/performance # 績效快照（?days=90）
POST /api/portfolio/{id}/optimise    # 均值-變異數最佳化（需 analyst 角色）
```

Migration `0086` 新增多幣別 append-only 現金帳本。台股交易以 TWD、美股／加密交易以
USD 自動產生原生幣別結算；修改交易會先沖銷舊結算再追加新分錄，刪除交易只追加反向
分錄，帳本紀錄本身不做覆寫或刪除。系統拒絕任何在交易時點造成負庫存的賣出，避免
持倉被歸零但現金仍虛增。舊交易會回填結算分錄，並按各投組／幣別推定「不讓歷史現金
跌破零」所需的最小期初入金，且以 `legacy_inferred` metadata 明確揭露這是推定值。

每日 `portfolio_snapshots` 同時保存個別部位、原生現金餘額、投組基準幣別下的持倉／
現金／淨清算價值，以及缺價標的；舊快照只保有 USD 總額，因此標記
`legacy_total_only`，不偽造不存在的歷史成分。投組頁面的「現金帳本」可新增現金異動、
查看負現金警示並以反向分錄更正；因子再平衡會直接使用帳本中的實際 TWD 現金，USD
等外幣預設凍結，不暗中換匯，另填的現金只作假設情境並明確標記。

### 分析（需 analyst 角色）

```
POST /api/analytics/dcf              # DCF 估值（敏感度網格 + bull/base/bear）
POST /api/analytics/var              # VaR（historical / parametric / monte_carlo）
POST /api/analytics/backtest         # 策略回測（sma_crossover / rsi_mean_reversion）
```

### AI 代理人

```
POST /api/ai/chat                    # SSE 串流對話（指定 persona + provider）
```

### 多專家討論

```
GET  /api/discussion/sessions        # 列出討論
POST /api/discussion/sessions        # 建立討論
POST /api/discussion/sessions/{id}/round     # 背景執行一輪專家發言（SSE）
POST /api/discussion/sessions/{id}/conclude  # 產生結論
GET  /api/discussion/auto-run/config # 讀取每日自動討論設定
PUT  /api/discussion/auto-run/config # 更新 auto-run topic / rules / personas
```

### 觀察清單 / 警報

```
GET  /api/watchlist                  # 列出觀察清單
POST /api/watchlist                  # 建立觀察清單
POST /api/watchlist/{id}/items       # 新增標的
DELETE /api/watchlist/{id}/items/{symbol}  # 移除標的
GET  /api/alerts                     # 列出價格警報
POST /api/alerts                     # 建立警報
DELETE /api/alerts/{id}              # 刪除警報
```

### 系統

```
GET  /api/system/version             # 版本與 GitHub release 狀態
POST /api/system/web-vital           # Core Web Vitals → Prometheus
```

---

## 技術棧

| 層級 | 技術 |
|------|------|
| **後端框架** | FastAPI 0.110+、Python 3.11+ |
| **資料庫** | PostgreSQL 15+（非同步，asyncpg）|
| **快取** | Redis 7（報價 15s、歷史 4h、基本面 24h）|
| **即時串流** | WebSocket（FastAPI 原生）+ Redis Pub/Sub + Kraken ticker pump |
| **前端框架** | React 18、TypeScript、Vite 6 |
| **UI 元件庫** | shadcn/ui、Tailwind CSS |
| **圖表** | lightweight-charts v4、Recharts |
| **狀態管理** | Zustand、TanStack Query |
| **量化計算** | NumPy、Pandas、SciPy |
| **排程** | APScheduler（US 10s、TW 60s、TW EOD、ingest、news sentiment、discussion auto-run / verifier）|
| **認證** | JWT（python-jose）、slowapi 限速 |
| **容器化** | Docker、Docker Compose、Kubernetes Helm |
| **可觀測性** | Prometheus（/metrics）、JSON 結構化日誌 |

---

## 與桌面版的差異

| 項目 | Fincept Terminal（桌面版）| Fincept Web（伺服器版）|
|------|--------------------------|------------------------|
| **語言/框架** | C++20 + Qt6 | Python（FastAPI）+ React |
| **部署方式** | 本機安裝（exe/dmg/.run）| Docker / Cloud Server |
| **多用戶** | 單機使用 | 原生多用戶支援 |
| **美股支援** | 部分（Yahoo Finance 等）| 深度整合（Polygon + FRED）|
| **台股支援** | 無 | 完整支援（TWSE + FinMind + MOPS）|
| **UI 存取** | 需安裝桌面客戶端 | 任何瀏覽器 |
| **API 對外開放** | 無 | REST API + WebSocket |
| **AI 代理人** | 無 | 19 人格、多 LLM provider、伺服器端多人共用 |
| **效能** | 原生二進制，最快 | 受網路延遲影響，可水平擴展 |

---

## 開發路線圖

| 狀態 | 里程碑 |
|------|--------|
| ✅ **完成** | Phase 0：專案架構、Docker Compose、PostgreSQL、nginx |
| ✅ **完成** | Phase 1：JWT 認證、API 金鑰、角色權限（viewer / analyst / admin）|
| ✅ **完成** | Phase 2：美股資料（Polygon → yfinance 瀑布、FRED 總體、S&P500 宇宙）|
| ✅ **完成** | Phase 3：台股資料（TWSE OpenAPI → FinMind 瀑布、法人、融資券）|
| ✅ **完成** | Phase 4：WebSocket 即時串流（Auth-first、Redis Pub/Sub、delta 抑制）|
| ✅ **完成** | Phase 5：APScheduler（美股 10s / 台股 60s、off-hours 節流）|
| ✅ **完成** | Phase 6：投資組合管理（多幣別 P&L、均攤成本、均值-變異數最佳化）|
| ✅ **完成** | Phase 7：CFA 分析引擎（DCF + 敏感度、VaR 三種方法、事件驅動回測）|
| ✅ **完成** | Phase 8：AI 代理人（19 人格、多 LLM provider、SSE 串流）|
| ✅ **完成** | Phase 9：完整 React UI（K 線圖、選股篩選器、個股詳情、Sidebar）|
| ✅ **完成** | Phase 10：Docker 生產強化（多階段 build、nginx 限速、Redis 認證）|
| ✅ **完成** | Phase 11：Kubernetes Helm chart（ingress、HPA、secrets）|
| ✅ **完成** | Phase 12：Prometheus metrics 中介層、JSON 結構化日誌 |
| ✅ **完成** | Phase 13：PWA（manifest、service worker、cache-first 靜態）|
| ✅ **完成** | Phase 14：觀察清單多清單 CRUD + 即時報價擴充 |
| ✅ **完成** | Phase 15：價格警報 CRUD + WebSocket 推送 |
| ✅ **完成** | Phase 16：Admin 頁面（使用者管理、角色升降、系統統計）|
| ✅ **完成** | Phase 17：AI 聊天頁面（SSE 串流、人格切換、多 LLM 供應商）|
| ✅ **完成** | Phase 18：Settings 頁面（個人資料、密碼、API 金鑰管理）|
| ✅ **完成** | Phase 19：TanStack Virtual 選股篩選器（大量列渲染）|
| ✅ **完成** | Phase 20：回測 UI（策略參數、股票曲線圖、指標表格）|
| ✅ **完成** | Phase 21：VaR UI（多資產輸入、方法選擇、結果視覺化）|
| ✅ **完成** | Phase 22：DCF UI（覆寫參數、敏感度表格、情境比較）|
| ✅ **完成** | Phase 23：整合測試（auth、portfolio、TW market API 測試套件）|
| ✅ **完成** | Phase 24：Ruff lint 修正（43 errors）、StockDetailPage 主題色修正 |
| ✅ **完成** | Phase 25：修正前端雙 /api 前綴錯誤、新增 analytics API 整合測試 |
| ✅ **完成** | Phase 26：PortfolioPage 主題色修正、新增 portfolio 延伸整合測試 |
| ✅ **完成** | Phase 27：CandlestickChart 主題感知修正、新增 US market API 整合測試 |
| ✅ **完成** | Phase 28：Crypto market（Kraken REST / WS、Top 20 universe、watchlist enrichment）|
| ✅ **完成** | Phase 29：資料歸檔（OHLCV、quote snapshots、fundamentals、news、sentiment scoring）|
| ✅ **完成** | Phase 30：多專家 discussion（背景 round、結論 synthesizer、per-user auto-run、自動驗證）|
| ✅ **完成** | Phase 31：系統版本檢查與 Core Web Vitals → Prometheus |
| ✅ **完成** | 選擇權分析：期限結構、IV 曲面、預期波動、P/C OI、翼端偏斜、最大痛點與完整度 |
| ✅ **完成** | 台股三表深度分析（XBRL 損益／資產負債／現金流、TTM、DuPont、現金含金量、同比訊號與資料完整度）|
| ✅ **完成** | 台股可解釋多因子研究（六因子、point-in-time 財報、受限 walk-forward、模型治理，以及風險／產業／單檔／換手／流動性受限投組）|
| ✅ **完成** | 有效日期台股商品主檔、債券 ETF 稅則、人工覆寫、因子母體與再平衡共用規則 |
| 🔄 **規劃中** | 接入 TWSE／TPEx 完整商品分類檔與下市生命週期、券商成交回報／paper trading、公司行動成本基礎與實際財報公告時間 |

---

## 公開每日圓桌

設定 `PUBLIC_DAILY_RESULTS_OWNER_EMAIL` 為一個既有且啟用每日自動圓桌的帳號 email，重新部署 backend 後，即可透過固定網址 `/daily` 免登入分享最近一次成功結果。空值代表停用；若帳號 email 變更，部署設定也必須同步更新。

公開 API 為 `GET /api/public/daily`，不接受 user ID、email 或 discussion ID 查詢參數。它只公開原始結論與前五輪自動發言，不包含規則、成本、內部 context、手動提問、後續輪次、事後檢討或歷史列表。回應允許 60 秒公開快取，頁面與 API 均設定 `noindex, nofollow`。

- Docker Compose：在 `.env` 設定變數後執行 `docker compose up -d --build backend frontend nginx`。
- Helm：設定 `env.backend.PUBLIC_DAILY_RESULTS_OWNER_EMAIL` 後執行既有的 chart upgrade/deploy 流程。

---

## 公開每日圓桌

設定 `PUBLIC_DAILY_RESULTS_OWNER_EMAIL` 為一個既有且啟用每日自動圓桌的帳號 email，重新部署後即可透過 `/daily` 免登入分享最近一次成功結果。空值代表停用。

公開 API 為 `GET /api/public/daily`，只公開結論與前五輪自動發言，不包含帳號資料、規則、內部 context、手動提問或事後檢討。

## 貢獻指南

歡迎貢獻新的資料連接器、AI 代理人、分析模組或前端元件。

- [回報 Bug](https://github.com/x812033727/finceptweb/issues)
- [功能建議](https://github.com/x812033727/finceptweb/discussions)

**特別歡迎以下貢獻：**
- 台股資料連接器（TEJ、公開資訊觀測站深度整合）
- 選擇權隱含波動率曲面計算
- 前端圖表元件
- 量化策略回測範例

---

## 授權

**雙重授權：AGPL-3.0（開源）+ 商業授權**

### 開源（AGPL-3.0）
- 個人、學術研究、非商業用途免費
- 作為網路服務使用時須開放修改後的原始碼

### 商業授權
- 商業使用請聯繫：**support@fincept.in**

---

## 致謝

本專案以 [Fincept Terminal](https://github.com/Fincept-Corporation/FinceptTerminal) 為基礎發展，感謝 Fincept Corporation 的開源貢獻。

台股資料整合感謝：
- [TWSE 臺灣證券交易所](https://www.twse.com.tw/) — 官方公開資料
- [FinMind](https://finmindtrade.com/) — 台灣金融開放資料平台
- [公開資訊觀測站](https://mops.twse.com.tw/) — 財報與重大訊息

---

<div align="center">

### **Your Thinking is the Only Limit. The Data Isn't.**

© 2025-2026 Fincept Web Contributors. Released under AGPL-3.0.

</div>
