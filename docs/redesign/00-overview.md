# FinceptWeb99 架構重規劃藍圖 — 總覽

> 2026-07-11 制定。本藍圖為**文件先行**:先審藍圖、核准後分批動工。所有改動將在 FinceptWeb99(https://fincept99.aibubu.cloud)驗證,不影響原站 FinceptWeb。

## 1. 目標與範圍決策

**目標**:在保留現有技術骨架的前提下,系統性提升三個面向 ——
1. **效能**:前端載入與渲染、API 延遲與快取、即時資料管線(三者全做)
2. **功能**:看盤圖表進階、AI 能力、投組回測、告警自動化(四方向全做)
3. **UI**:專業金融終端風精緻化(保留深色終端 DNA,打磨到 Bloomberg/TradingView 級一致性)

**明確不做**:
- 不換技術棧、不大改架構(FastAPI + React + PG/Timescale + Redis 保留)—— 系統 ~158K LOC 後端 + 38K LOC 前端、~3000 個測試,全部重寫風險極高
- 不引入 Celery / Kafka / 微服務拆分
- 不挑戰 `docs/architecture-notes.md` 記載的刻意非抽象化決策(無泛用 httpx wrapper、無 cached_fetch)

## 2. 現況一頁摘要

| 層 | 現況 | 體質 |
|---|------|------|
| 後端 | FastAPI async 單體,16 個 feature router + fat services(96 檔/35.5K LOC),獨立 finmind 子系統(109 檔) | 快取紀律佳、降級鏈完整;但排程器每 worker 重複跑、WS 管線有靜默死亡風險 |
| 資料 | TimescaleDB(3 組 hypertable)+ Redis(3 層快取/pub-sub/token bucket),63+21 條 Alembic 遷移 | 索引未經審計、主庫壓縮未開、FinMind 遷移有多 pod 競態 |
| 前端 | React 18 + Vite + Tailwind/shadcn,21 頁全 lazy,Zustand + TanStack Query,PWA | 工程底子好;但 696 處硬編色、雙圖表庫色彩不同源、WS tick 重渲整頁 |
| 部署 | docker-compose 單機(fincept99 跳過 prometheus/grafana),host nginx + certbot | 可觀測性斷線 —— 指標有產出、無人收 |

## 3. 三軌藍圖(詳見各章節)

### 軌 1:[後端架構與效能](01-backend-perf.md)
12 個弱點診斷(W1-W12,附 file:line 證據),最嚴重:排程器在 2 個 uvicorn worker 各跑一份(上游 API 配額雙倍燒)、WS pubsub listener 無重連(Redis 瞬斷=行情永久靜音)、全站無 gzip。目標架構核心 = **排程器獨立容器** + WS 管線重構(反向索引/send queue/MGET)+ DB 池預算化 + 可觀測性重啟用。路線 P0→P5 六階段。

### 軌 2:[前端 UI + 效能](02-frontend-ui.md)
設計系統重整:語意色 token 架構(含**台股紅漲綠跌 / 國際綠漲紅跌可切換**的 `data-market-colors` 機制)、696 處硬編色 codemod、`chartTheme.ts` 統一雙圖表庫、`<Num>`/`<DeltaText>` 數字原子、密度系統。效能:manualChunks、WS quoteStore rAF 批次(解 tick 重渲整頁)、虛擬化擴張、sw.js 部署斷鏈修復。路線 PR-1→PR-10 十批。

### 軌 3:[功能路線圖](03-features.md)
四方向 20 個功能提案,全部建於既有子系統之上(已逐一核對程式碼)。P0 起手式:A3 全螢幕看盤、A1 技術指標疊加、C1 投組風險儀表板、D4 策略監控告警、D5 告警歷史。⚠️ LINE Notify 已停役,通知通道改用 LINE Messaging API。

### 跨軌交叉引用
- A5 畫線工具的水平線 → 一鍵轉 D1 進階告警
- B5 AI 投組健檢 → 依賴 C1 風險儀表板的計算路徑
- 軌 3 所有圖表類功能 → 依賴軌 2 PR-4 的 chartTheme 統一(先做地基再蓋樓)
- 軌 3 D2/D3 通知通道 → 受益於軌 1 P2 排程器分離(告警 push 改走 Redis pub/sub 後天然支援多通道)

## 4. 整體時程建議(並行性)

```
第一波(可並行):
  軌1 P0 觀測基線(S) ─┐
  軌1 P1 止血批(S)   ─┼─ 互不衝突,可同週出貨
  軌2 PR-1 token 地基  ─┤
  軌2 PR-2 色彩 codemod ─┘
第二波:
  軌1 P2 排程器分離(M)   ‖  軌2 PR-3/4/5(排版原子+圖表主題+chunking)
  軌3 P0 功能(A3/D5/D4)  ‖  (S 級,穿插做)
第三波:
  軌1 P3 WS 管線 + P4 DB 調優  ‖  軌2 PR-6~9  ‖  軌3 A1/C1/D1/B1
第四波:
  軌1 P5 收尾  ‖  軌2 PR-10  ‖  軌3 P1-P2 功能陸續上
```

規則:**每批 PR 獨立可出貨、全量測試綠燈才合併**;跨軌依賴只有第 3 節列出的四條,其餘可自由排程。

## 5. 驗證方法論(每批通用)

1. 後端:`pytest tests/ --asyncio-mode=auto` 全綠(~3000 tests);涉及 WS/排程的批次另加負載/斷線演練
2. 前端:`vitest` 全綠 + `tsc -b` + `eslint`;視覺批次附截圖對照
3. 部署驗證:在 fincept99 上 `docker-compose up -d --build` 後 `curl /api/health` + 手動 smoke
4. 效能批次:P0 先建 Prometheus 基線,之後每批對照(cache hit-rate、p95、bundle size、WS dispatch latency)
