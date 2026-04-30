# Fincept Web Terminal

<div align="center">

[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-C06524)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18+-61DAFB?logo=react&logoColor=black)](https://react.dev/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)

### **Your Thinking is the Only Limit. The Data Isn't.**

專業級金融智能平台——伺服器版，支援 CFA 級分析、AI 自動化，以及美股、台股與全球市場數據整合。

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
│   │   ├── auth/         # JWT 登入／註冊／刷新／登出、API 金鑰
│   │   ├── us_market/    # 美股報價、歷史、基本面、選擇權、總經、新聞
│   │   ├── tw_market/    # 台股行情、歷史、法人、融資券、月營收、新聞
│   │   ├── crypto_market/ # Kraken-backed crypto 報價、歷史、篩選與搜尋
│   │   ├── portfolio/    # 持倉、交易紀錄、P&L、最佳化
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
│   ├── db/               # Alembic 0001-0020、引擎、seed
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
POST /api/auth/register              # 註冊
POST /api/auth/login                 # 登入（回傳 access token）
POST /api/auth/refresh               # 刷新 access token（httpOnly cookie）
POST /api/auth/logout                # 登出（撤銷 refresh token）
GET  /api/auth/me                    # 取得目前使用者
PATCH /api/auth/me                   # 更新個人資料
GET  /api/auth/api-keys              # 列出 API 金鑰
POST /api/auth/api-keys              # 建立 API 金鑰
DELETE /api/auth/api-keys/{id}       # 刪除 API 金鑰
```

### 美股

```
GET  /api/us/quote/{ticker}          # 即時報價
GET  /api/us/history/{ticker}        # 歷史 K 線（?period=1y&interval=1d）
GET  /api/us/fundamentals/{ticker}   # 基本面資料
GET  /api/us/options/{ticker}        # 選擇權鏈
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
GET  /api/tw/news/{symbol}           # 個股新聞
GET  /api/tw/indices                 # 大盤指數
```

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
GET  /api/portfolio/{id}/performance # 績效快照（?days=90）
POST /api/portfolio/{id}/optimise    # 均值-變異數最佳化（需 analyst 角色）
```

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
| 🔄 **規劃中** | 選擇權鏈分析頁面、隱含波動率曲面 |
| 🔄 **規劃中** | 台股 XBRL 財報深度整合 |
| 🔄 **規劃中** | 多因子選股模型（ML）|

---

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
