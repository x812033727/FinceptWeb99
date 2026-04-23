# Fincept Web Terminal

<div align="center">

[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-C06524)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18+-61DAFB?logo=react&logoColor=black)](https://react.dev/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)

### **Your Thinking is the Only Limit. The Data Isn't.**

專業級金融智能平台——伺服器版，支援 CFA 級分析、AI 自動化，以及美股、台股與全球市場數據整合。

[📚 文件](#文件) · [🚀 快速啟動](#快速啟動) · [💬 討論](https://github.com/x812033727/finceptweb/discussions) · [🐛 回報問題](https://github.com/x812033727/finceptweb/issues)

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
│   ├── api/              # REST API 路由
│   │   ├── us_market/    # 美股模組
│   │   ├── tw_market/    # 台股模組
│   │   ├── portfolio/    # 投資組合管理
│   │   ├── analytics/    # 量化分析引擎
│   │   └── ai_agents/    # AI 代理人
│   ├── data/             # 資料連接器
│   │   ├── us/           # 美股資料源（Polygon、Yahoo Finance、FRED）
│   │   └── tw/           # 台股資料源（TWSE OpenAPI、TEJ、FinMind）
│   └── models/           # 資料模型
├── frontend/             # React 前端
│   ├── components/       # UI 元件
│   │   ├── charts/       # 圖表（K 線、成交量、指標）
│   │   ├── screener/     # 選股工具
│   │   └── dashboard/    # 儀表板
│   └── pages/            # 頁面路由
├── docker-compose.yml    # 一鍵部署
└── docs/                 # 說明文件
```

---

## 功能特色

| 功能 | 說明 |
|------|------|
| 📊 **CFA 級分析** | DCF 估值模型、投資組合最佳化、風險指標（VaR、Sharpe Ratio）、衍生品定價 |
| 🇺🇸 **美股整合** | 即時報價、財報分析、SEC 申報資料、選擇權鏈、ETF 持股明細、S&P 500 成分 |
| 🇹🇼 **台股整合** | 上市櫃即時行情、籌碼分析、法人買賣超、融資融券、財報（XBRL）、MSCI 成分 |
| 🤖 **AI 代理人** | 37 種分析框架（Buffett、Graham、Lynch 等投資大師風格）；支援 OpenAI、Anthropic、Gemini、Ollama 等多家 LLM |
| 🌐 **100+ 資料連接器** | Yahoo Finance、Polygon、FRED、IMF、World Bank、FinMind、TWSE OpenAPI、TEJ |
| 📈 **技術分析** | K 線圖、均線、RSI、MACD、布林通道、KD 指標、即時 WebSocket 串流 |
| 🔬 **量化分析** | 因子模型、統計套利、回測引擎、隨機波動率、固定收益定價（QuantLib）|
| 🧠 **AI 量化實驗室** | ML 選股模型、強化學習交易代理、HFT 回測 |
| 👥 **多用戶管理** | JWT 認證、角色權限控管、API 金鑰管理、使用量追蹤 |
| 🐳 **容器化部署** | Docker Compose 一鍵啟動，支援 Kubernetes 水平擴展 |

---

## 市場覆蓋

### 🇺🇸 美股（US Equities）

| 類別 | 內容 |
|------|------|
| **交易所** | NYSE、NASDAQ、AMEX、OTC |
| **指數** | S&P 500、NASDAQ 100、Dow Jones、Russell 2000、VIX |
| **資料類型** | 即時報價、歷史 OHLCV、基本面、財報、SEC 申報（10-K/10-Q/8-K） |
| **衍生品** | 選擇權鏈、隱含波動率曲面、到期行事曆 |
| **ETF／基金** | 持股明細、溢折價、淨值追蹤 |
| **資料來源** | Polygon.io、Yahoo Finance、FRED、SEC EDGAR、CBOE |

### 🇹🇼 台股（Taiwan Equities）

| 類別 | 內容 |
|------|------|
| **交易所** | 臺灣證券交易所（TWSE）、證券櫃檯買賣中心（TPEx）|
| **指數** | 加權股價指數（TAIEX）、OTC 指數、半導體類股指數、電子類股指數 |
| **資料類型** | 即時行情、歷史 OHLCV、每日成交統計、借券資料 |
| **籌碼分析** | 外資（FINI）、投信、自營商買賣超；融資融券餘額 |
| **財報資料** | 月營收、季報、年報（XBRL 格式）、股利政策 |
| **資料來源** | TWSE OpenAPI、FinMind、TEJ（台灣經濟新報）、公開資訊觀測站 |

---

## 快速啟動

### 方式一：Docker Compose（推薦）

```bash
git clone https://github.com/x812033727/finceptweb.git
cd finceptweb

# 複製並設定環境變數
cp .env.example .env
# 在 .env 中填入您的 API 金鑰（見下方說明）

# 啟動所有服務
docker compose up -d
```

服務啟動後：
- **前端**：http://localhost:3000
- **API 文件**：http://localhost:8000/docs
- **API（ReDoc）**：http://localhost:8000/redoc

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
pip install -r requirements.txt

# 資料庫初始化
alembic upgrade head

# 啟動 API 伺服器
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

#### 前端

```bash
cd frontend
npm install
npm run dev
```

---

## 環境變數設定

在根目錄的 `.env` 中設定以下金鑰：

```env
# 資料庫
DATABASE_URL=postgresql://user:password@localhost:5432/finceptweb
REDIS_URL=redis://localhost:6379

# 美股資料
POLYGON_API_KEY=your_polygon_key          # polygon.io
YAHOO_FINANCE_ENABLED=true                # 免費，無需金鑰

# 台股資料
FINMIND_TOKEN=your_finmind_token          # finmindtrade.com
TWSE_API_ENABLED=true                     # 官方免費 API

# AI / LLM
OPENAI_API_KEY=your_openai_key
ANTHROPIC_API_KEY=your_anthropic_key
GEMINI_API_KEY=your_gemini_key            # 可選
OLLAMA_HOST=http://localhost:11434        # 本地 LLM

# 總體經濟
FRED_API_KEY=your_fred_key               # 美聯準免費 API

# 安全性
JWT_SECRET_KEY=your_super_secret_key
JWT_ALGORITHM=HS256
```

---

## API 端點概覽

### 美股

```
GET  /api/us/quote/{ticker}              # 即時報價
GET  /api/us/history/{ticker}            # 歷史 K 線
GET  /api/us/fundamentals/{ticker}       # 基本面資料
GET  /api/us/financials/{ticker}         # 財報（損益表、資產負債、現金流）
GET  /api/us/options/{ticker}            # 選擇權鏈
GET  /api/us/screener                    # 選股篩選器
GET  /api/us/indices                     # 指數行情
```

### 台股

```
GET  /api/tw/quote/{symbol}              # 即時行情（e.g., 2330）
GET  /api/tw/history/{symbol}            # 歷史 K 線
GET  /api/tw/fundamentals/{symbol}       # 基本面
GET  /api/tw/financials/{symbol}         # 財報
GET  /api/tw/institutional/{symbol}      # 法人買賣超
GET  /api/tw/margin/{symbol}             # 融資融券
GET  /api/tw/revenue/{symbol}            # 月營收
GET  /api/tw/screener                    # 選股篩選器
GET  /api/tw/indices                     # 大盤指數
```

### 分析

```
POST /api/analytics/dcf                  # DCF 估值
POST /api/analytics/portfolio/optimize   # 投資組合最佳化
POST /api/analytics/risk/var             # VaR 計算
POST /api/analytics/backtest             # 策略回測
```

### AI 代理人

```
POST /api/ai/analyze                     # 請 AI 分析個股
POST /api/ai/portfolio/review            # AI 投資組合評審
GET  /api/ai/agents                      # 列出可用代理人
```

---

## 技術棧

| 層級 | 技術 |
|------|------|
| **後端框架** | FastAPI 0.110+、Python 3.11+ |
| **資料庫** | PostgreSQL 15（主要）、TimescaleDB（時間序列）|
| **快取** | Redis 7 |
| **即時串流** | WebSocket（FastAPI 原生）|
| **前端框架** | React 18、TypeScript、Vite |
| **UI 元件庫** | shadcn/ui、Tailwind CSS |
| **圖表** | TradingView Lightweight Charts、Recharts |
| **量化計算** | QuantLib-Python、NumPy、Pandas、SciPy |
| **ML 框架** | scikit-learn、PyTorch（選用）|
| **容器化** | Docker、Docker Compose、Kubernetes（選用）|
| **認證** | JWT（python-jose）、OAuth2 |

---

## 與桌面版的差異

| 項目 | Fincept Terminal（桌面版）| Fincept Web（伺服器版）|
|------|--------------------------|------------------------|
| **語言/框架** | C++20 + Qt6 | Python（FastAPI）+ React |
| **部署方式** | 本機安裝（exe/dmg/.run）| Docker / Cloud Server |
| **多用戶** | 單機使用 | 原生多用戶支援 |
| **美股支援** | 部分（Yahoo Finance 等）| 深度整合（Polygon + EDGAR）|
| **台股支援** | 無 | 完整支援（TWSE + FinMind）|
| **UI 存取** | 需安裝桌面客戶端 | 任何瀏覽器 |
| **API 對外開放** | 無 | REST API + WebSocket |
| **AI 代理人** | 37 種（本地端）| 37 種（伺服器端，可多人共用）|
| **效能** | 原生二進制，最快 | 受網路延遲影響，可水平擴展 |

---

## 開發路線圖

| 時程 | 里程碑 |
|------|--------|
| **已完成** | 專案架構設計、API 框架、Docker 配置、文件 |
| **Q2 2026** | 美股核心 API（報價、歷史、財報、選股篩選器） |
| **Q2 2026** | 台股核心 API（行情、法人、籌碼、月營收） |
| **Q3 2026** | 前端儀表板（K 線圖、投資組合、選股工具） |
| **Q3 2026** | AI 代理人整合、量化分析引擎 |
| **Q4 2026** | 選擇權分析、多帳戶投資組合管理 |
| **Q4 2026** | Kubernetes 部署、機構功能 |
| **2027** | 行動版（PWA）、社群市集 |

---

## 貢獻指南

歡迎貢獻新的資料連接器、AI 代理人、分析模組或前端元件。

- 閱讀 [CONTRIBUTING.md](docs/CONTRIBUTING.md)
- [回報 Bug](https://github.com/x812033727/finceptweb/issues)
- [功能建議](https://github.com/x812033727/finceptweb/discussions)

**特別歡迎以下貢獻：**
- 台股資料連接器（TEJ、公開資訊觀測站）
- 美股基本面分析模組
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
