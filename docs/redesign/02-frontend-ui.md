# 前端 UI 重設計 + 前端效能藍圖

> 本文件是[架構重規劃藍圖](00-overview.md)的前端章節。設計方向:**專業金融終端風精緻化** —— 保留深海軍藍終端 DNA(`--background: 222 47% 8%`、sky-blue primary),向 Bloomberg / TradingView 級的一致性與密度打磨,不做亮色 SaaS 化。

## 1. UI 現況診斷

### 1.1 已具備的底子(保留)

- **Token 化基礎正確**:`frontend/src/index.css` 以 HSL CSS vars 定義 17 個 shadcn 語意色,light theme 走 `[data-light]` 屬性覆寫;`frontend/src/components/charts/CandlestickChart.tsx:24-50` 已有 `hslVarToRgb()` 執行期讀 token 餵給 canvas 圖表——這是全站圖表主題化的正確種子。
- **終端密度已成形**:topbar `h-10`、desktop sidebar `lg:w-48`、`text-xs`/`text-[10px]` 標籤、`OverseasIndicators` 的 `font-mono tabular-nums` 單行列(`frontend/src/pages/DashboardPage.tsx:219-229`)是全站應統一的典範。
- **路由層工程成熟**:21 頁 lazy + hover prefetch(`frontend/src/pageLoaders.ts`)、每路由 ErrorBoundary、CommandPalette、WS 單例含 backoff/jitter。

### 1.2 不精緻之處(具體證據)

1. **漲跌色失控**:`text-red-400`×66、`text-green-400`×38、`text-red-300`×33、`text-green-600`×10……共 13 種深淺;硬編 palette utility 共 **696 處**。`frontend/tailwind.config.ts:50-51` 的 `positive/negative` 是硬編 hex(`#22c55e`/`#ef4444`),不隨主題、也僅被用 22 次(HoldingsTable、AnalyticsPage 等 5 檔)。
2. **圖表色各自為政**:TSX 內共 ~20 種硬編 hex——`#f59e0b`×10、`#ef4444`×9、`#22c55e`×9、`#10b981`×8、`#3b82f6`×6、`#6366f1`×5。`RevenuePanel.tsx:34` 營收長條用 **indigo #6366f1**(非品牌色);`CandlestickChart.tsx:87-94,155` K 線與量能色硬編且與 Recharts 面板色不同源。
3. **品牌不一致**:`frontend/public/manifest.webmanifest` `theme_color: "#6366f1"`(indigo)vs primary `hsl(199 89% 48%)` ≈ `#0ea5e9`;`background_color: "#0f1117"` 也非 `--background` 實值。
4. **數字排版不一**:StockDetailPage 主價有 `tabular-nums`(:174),但 DashboardPage `IndexCard` 價格(:39-43)沒有;全站僅 47 檔用到 `font-mono`/`tabular-nums`,表格數字對齊靠運氣。
5. **Dashboard 質感落差**:Quick Access 卡用 emoji 圖示(`DashboardPage.tsx:405-413` 🇺🇸🇹🇼₿🔍⭐)vs 全站 lucide 線性圖示;sentiment badge 硬編 `bg-green-900/30 text-green-300`(:78-80)在 light theme 下必然失衡。
6. **巨石元件**:`DiscussionPage.tsx` ~1990 行(全站最大);`AIPage.tsx` 705 行、`ScreenerPage.tsx` 582 行。
7. **雜項**:`tailwind.config.ts:5` `darkMode: ["class"]` 與實際 `[data-light]` 機制脫節(死設定);StockDetailPage 內 `"法人買賣超"`、`"融資融券"` 等字串未走 i18n(:122-135)。

## 2. 設計系統重整

### 2.1 語意色 Token 架構(`frontend/src/index.css` 擴充)

```css
:root {
  /* Surfaces — 邊框優先的終端層級,不用陰影 */
  --surface-0: 222 47% 8%;   /* page (=background) */
  --surface-1: 222 47% 11%;  /* card */
  --surface-2: 222 47% 14%;  /* nested panel / input */
  --surface-3: 222 47% 17%;  /* popover / dropdown */
  --border-subtle: 222 47% 15%;
  --border-strong: 222 47% 22%;
  /* Status(非市場方向)*/
  --success: 152 60% 42%;  --warning: 38 92% 50%;
  --danger: 0 72% 51%;     --info: 199 89% 48%;
  /* 市場方向 — 由 data-market-colors 決定 */
  --up: var(--mkt-up);  --down: var(--mkt-down);  --flat: 215 20% 55%;
  /* 圖表序列色(分類 6 色,dark 校準)*/
  --chart-1: 199 89% 55%; --chart-2: 38 92% 55%; --chart-3: 262 70% 65%;
  --chart-4: 152 60% 48%; --chart-5: 330 70% 60%; --chart-6: 215 20% 60%;
}
[data-market-colors="intl"] { --mkt-up: 152 65% 45%; --mkt-down: 0 75% 55%; }
[data-market-colors="tw"]   { --mkt-up: 0 75% 55%;  --mkt-down: 152 65% 45%; } /* 台股紅漲綠跌 */
```

**市場慣例切換**:`themeStore` 新增 `marketColorMode: "auto" | "tw" | "intl"`;`auto` = 跟隨 i18n 語言(zh-TW → 紅漲綠跌)。所有漲跌一律用 `text-up` / `text-down` / `bg-up/10`(Tailwind 映射 `up: "hsl(var(--up))"`),元件端**不再判斷紅綠**,只判斷方向——慣例切換即全站(含 K 線)一鍵翻轉。`tailwind.config.ts` 的 `positive/negative` 改指向 `hsl(var(--up))`/`hsl(var(--down))` 作過渡別名。Settings 頁提供覆寫。

### 2.2 排版與密度

- **數字**:新增 utility `.num { font-variant-numeric: tabular-nums; }` + 約定「所有價格/百分比/量值 = `font-mono tabular-nums`」;抽出 `<Num>` / `<DeltaText value>` 原子元件(自帶 `+`/`−` 號、`text-up/down/flat`、右對齊),取代散落的 `isPos ? "text-green-400" : "text-red-400"` 三元式。
- **字級階**:固定 7 級 `10/11/12/14/16/20/24px`;標題群組沿用現有 `text-[10px] uppercase tracking-wider` 段落標樣式(`Sidebar.tsx:135` 已是)。
- **密度**:CSS var `--row-h`(compact 28px / comfortable 36px),`[data-density="compact"]` 切換,由 Settings 控制;表格 row 用 `h-[var(--row-h)]`。
- **層級語言**:dark 模式零 box-shadow,層級 = surface 階梯 + border(`border-subtle` 內部分隔、`border-strong` 卡片外框);hover = `border-primary/40`(DashboardPage IndexCard 已用,標準化之)。
- **動效**:120–200ms `ease-out` 上限;新增 `animate-flash-up/down`(quote tick 時背景 300ms 淡出脈衝);全域尊重 `prefers-reduced-motion`。

### 2.3 圖表主題統一

新建 `frontend/src/lib/chartTheme.ts`:把 `CandlestickChart.tsx` 的 `hslVarToRgb`/`getChartColors` 搬出,輸出 `getChartTheme()` → `{ up, down, grid, text, series[6] }`(讀 CSS vars)。lightweight-charts 用 rgb 字串;Recharts 直接 `hsl(var(--chart-1))`。CandlestickChart 的 `#22c55e/#ef4444/#16a34a55/#dc262655` 全數改讀 theme,並訂閱 `marketColorMode` 重繪。Recharts 各面板包一層 `<ChartTooltip>`(統一 `contentStyle`)。

### 2.4 遷移策略(696 處硬編色)

可 codemod 的機械映射(sed / ts-morph,逐檔可 review):

| 現況 | 目標 | 備註 |
|---|---|---|
| `text-green-400/500/600` + 漲跌語境 | `text-up` | 佔多數 |
| `text-red-400/500/600` + 漲跌語境 | `text-down` | |
| `bg-green-900/30 text-green-300 border-green-800/50` | `bg-success/10 text-success border-success/30` | sentiment badge |
| `bg-green-500`(WsStatus 連線燈, `AppLayout.tsx:34`) | `bg-success` | **語意是成功非上漲** — 需人工分流 |
| `text-amber-*`/`#f59e0b` | `text-warning` / `--chart-2` | |

流程:先 grep 產出逐檔清單 → 標注「市場方向 vs 狀態」語意(唯一需要人工判斷的軸)→ codemod 套用 → 快照測試。ESLint 加 `no-restricted-syntax` 規則禁止新的 `text-(red|green)-\d`。

## 3. 版面精緻化(保留終端 DNA)

- **Dashboard**:emoji 卡 → lucide 圖示 + 左色條;`IndexCard` 價格補 `font-mono tabular-nums` + tick 迷你 sparkline(純 SVG,免 Recharts);右欄五個新聞/公告區塊統一為單一「情報流」卡片組(共用 header 樣式),減少五次重複的 section 標題;歡迎區壓縮為單行 status bar。
- **Market / StockDetail**:StockDetail 主價區已接近 TradingView 水準,補 tick flash 動效;stats sidebar 的 `StatRow` 換 `<Num>`;TW 硬編中文標籤入 i18n;MarketPage 表格套 `--row-h` compact 列 + 右對齊數字欄。
- **Screener**:582 行拆 `FilterBar` / `ResultsTable`;結果表已虛擬化,補 sticky header + 欄寬 `tabular-nums` 對齊;若導入排序/欄位控制,啟用已安裝的 `@tanstack/react-table`(headless,不增 UI 重量),否則移除該依賴。
- **Discussion / AI**:DiscussionPage 拆為 `discussion/` 子模組(訊息流、控制列、sweep 卡已部分抽出);訊息流維持虛擬化;AI 頁 705 行同樣拆 panel。
- **Admin / Settings**:admin 卡片群統一 surface-1 + `border-subtle` 分隔;Settings 新增「市場漲跌色慣例」與「密度」兩項(吃 2.1/2.2 的 token)。

## 4. 前端效能藍圖

1. **manualChunks**(`vite.config.ts` build.rollupOptions):`vendor-react`(react/react-dom/react-router/zustand/@tanstack/react-query/i18next)、`vendor-recharts`(recharts+d3,~130KB gz,只被 19 個 lazy 檔引用,切出後首屏零負擔)、`vendor-lwc`(lightweight-charts ~45KB gz)、`vendor-radix`。順帶把 `chunkSizeWarningLimit: 1500` 降回 800 當守門。
2. **關鍵路徑**:LoginPage 已 eager(正確);在 `App.tsx` 的 `silentRefresh()` 進行中同步呼叫 `pageLoaders.dashboard()`,讓 booting 空窗期預熱 dashboard chunk;`index.html` 加 `modulepreload` vendor-react。
3. **圖表庫決策**:**維持雙庫但統一主題與 chunk 隔離**。lightweight-charts 無 pie/bar/radar,無法收斂;ECharts 全量 ~300KB gz、按需 ~90KB,遷移 19 檔成本高於收益。中期把 `HealthMetricsSparkline` 類小圖改純 SVG,逐步縮小 Recharts 面積。
4. **虛擬化擴張**:`@tanstack/react-virtual` 目前僅 DiscussionPage、ScreenerPage;擴至 MarketPage movers 表、WatchlistPage、Admin 各清單(>50 列者)。
5. **TanStack Query**:全域 `staleTime 15s` 合理;補 per-class `gcTime`(fundamentals 24h、news 30m);history query 加 `placeholderData: keepPreviousData` 消除切 period 白屏;quote 類已有 `refetchInterval: 30s`,改為 WS 存在時停輪詢(`enabled: !wsConnected`)。
6. **WS 渲染批次**:StockDetailPage 每 delta 觸發 4 個 `setState`(:83-89)且重渲整頁。改為模組級 `quoteStore`(zustand,keyed by `SYMBOL:MARKET`),`useWebSocket` 的 delta 寫入 buffer、`requestAnimationFrame` 合併 flush 一次 `setState`;元件以 selector 訂閱單一 symbol,價格區抽成 `<LiveQuoteHeader>` 隔離重渲範圍,K 線容器不再陪跑。多列頁(watchlist)天然受益:一 tick 只重渲一列。
7. **sw.js**:啟用 `navigation preload`;API_CACHE 加 LRU 上限(~100 entries)防無限成長;**部署斷鏈修復**——目前 `skipWaiting()` + 版本化 cache purge 會讓舊 build 的 lazy chunk 404,需在前端監聽 `vite:preloadError` → 提示後整頁 reload(與現有 `UpdateBadge` 整合);`theme_color` 改 `#0ea5e9`、`background_color` 對齊 `--background`。
8. **icon/圖片**:lucide-react 已 tree-shake(維持具名 import 即可);dashboard emoji 移除後全站無點陣圖,`icon-512.svg` 補一個真 PNG maskable 供 Android 安裝。

## 5. 遷移路線圖(每批獨立可出貨,Vitest 全綠)

| 批次 | 內容 | 性質 |
|---|---|---|
| **PR-1 Token 地基** | index.css 新 tokens + tailwind 映射 + `data-market-colors` 機制 + `chartTheme.ts` + manifest 色修正。純新增,零視覺變化 | 手工,小 |
| **PR-2 漲跌色 codemod** | `text-green/red-*` → `text-up/down`(市場語境)、`text-success/danger`(狀態語境);更新受影響測試(HoldingsTable.test 等 assert class 名) | codemod + 人工分流 |
| **PR-3 排版/密度原子** | `<Num>` `<DeltaText>` 元件、`--row-h`、Settings 加慣例/密度開關 | 手工 |
| **PR-4 圖表主題統一** | CandlestickChart + 19 個 Recharts 檔改讀 chartTheme,清除 20 種硬編 hex | 半 codemod |
| **PR-5 效能:chunking** | manualChunks + boot 期 dashboard 預熱 + `vite:preloadError` reload | 手工,小 |
| **PR-6 WS quoteStore** | rAF 批次 + selector 訂閱 + `<LiveQuoteHeader>` 抽離 | 手工,需 WS mock 測試 |
| **PR-7 頁面打磨 A** | Dashboard(emoji→lucide、情報流合併)、StockDetail(i18n 補洞、flash 動效) | 手工 |
| **PR-8 頁面打磨 B + 拆檔** | DiscussionPage / AIPage / ScreenerPage 模組化(先純搬移不改行為,測試不動) | 手工 |
| **PR-9 Query/虛擬化** | gcTime 分級、keepPreviousData、watchlist/market/admin 虛擬化 | 手工 |
| **PR-10 sw.js + 收尾** | navigation preload、API LRU、react-table 去留、ESLint 禁硬編色規則 | 手工,小 |

**排序原理**:PR-1/2 是一切視覺工作的前提;PR-5/6 效能項無 UI 依賴可並行;頁面打磨(7/8)必須在 token 落地後,否則打磨成果會被 codemod 二次改寫。

## 6. 關鍵實作檔案

- `frontend/src/index.css`(token 架構主戰場)
- `frontend/tailwind.config.ts`(色彩映射、`positive/negative` 別名重導)
- `frontend/src/components/charts/CandlestickChart.tsx`(`hslVarToRgb` 抽出為 chartTheme.ts 的種子)
- `frontend/src/hooks/useWebSocket.ts`(quoteStore + rAF 批次改造點)
- `frontend/vite.config.ts`(manualChunks / 效能地基)
