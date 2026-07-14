# R6 設計系統 —— 已交付參考(「升級版金融終端」)

> **這是現行、與程式碼同步的設計系統文件。** 規劃期的 `02-frontend-ui.md` 描述的是重設計*之前*的狀態(`--background: 222 47% 8%`、696 硬編色、`darkMode:["class"]`、DiscussionPage ~1990 行),已不符現況——保留作歷史規劃紀錄,實作請以本文件為準。
>
> 交付於 2026-07-14,PR #91–#109(19 支),origin/main = b3114a4。全部前端變更。

---

## 1. 分層架構(改動優先序 = 由下而上)

| 層 | 檔案 | 換這裡會影響 |
|---|---|---|
| **值層(palette / type / spacing / elevation)** | `frontend/src/index.css` + `frontend/tailwind.config.ts` | 全站 + 圖表,零元件改動 |
| **原語** | `frontend/src/components/ui/*` | 所有頁面繼承 |
| **Shell** | `frontend/src/components/layout/{AppLayout,Sidebar,BottomNav}.tsx` | 導覽/版面框架 |
| **頁面** | `frontend/src/pages/*` | 各頁採用上面三層 |

**鐵則:能在值層一次生效的(色/字級/間距/elevation),就別在元件裡硬幹。**

---

## 2. Token(`index.css` `:root`,shadcn 式空格分隔 HSL)

### 色彩 —— R6 石墨藍
- `--background 222 26% 6%`、`--foreground 210 20% 93%`、`--primary 200 92% 52%`(azure)。
- **Surface ladder(邊框驅動、深色零陰影)**:`--surface-0 6%` → `-1 9.3%`(=`--card`,鎖步)→ `-2 12.6%` → `-3 16%`。`--border-subtle / --border / --border-strong`。
- **Status(獨立於漲跌)**:`--success / --warning / --danger / --info`。
- **漲跌(經 `--mkt-up/--mkt-down` 間接)**:`--up / --down / --flat`。由 `[data-market-colors="tw|intl"]` 翻轉(tw 紅漲綠跌)。
- **Chart 系列**:`--chart-1..6`(categorical,勿當語意色用)。

### 字級 —— role 命名(`tailwind.config.ts` fontSize)
`micro 10` · `label 11(+.06em)` · `data 12`(預設終端 body/表格格)· `body 13`(閱讀文字)· `heading 14` · `stat 20` · `title 24` · `display 30`。大字級帶負字距=終端數字質感。**新碼用 role token;`text-xs/sm` 為既有相容保留。`text-[10px]` 已被 ESLint 禁(用 `text-micro`)。**

### 間距節奏(`tailwind.config.ts` spacing)
`field 8` · `stack 12` · `gutter 16` · `page 24` · `section 24`。**頁面約定**:`p-gutter sm:p-page`、`space-y-stack sm:space-y-section`。

### Elevation
邊框 ladder 為主深度;`shadow-highlight`(`--highlight-top` inset 1px 高光)給 raised 面;`shadow-popover / shadow-overlay` **僅浮動層**(dropdown/sheet/drawer/dialog)。`--nav-h 3.5rem` 單一來源 bottom-nav 高度。

### 字型
`font-sans`=Inter/system-ui、`font-mono`=JetBrains Mono/ui-monospace(數字)。目前指向 system 字型(自 host webfont 為未做的可選 PR)。

---

## 3. 主題(屬性驅動,無 `.dark` class)

`store/themeStore.ts` 在 `<html>` 蓋屬性並持久化:
- **`[data-light]`** — 深色為預設,此屬性覆寫全 token(冷紙底、白卡、加深 status/chart)。
- **`[data-market-colors="tw|intl"]`** — 漲跌翻轉。`[data-light][data-market-colors]` combos 明列以贏 specificity。
- **`[data-density="compact"]`** — `--row-h` 36→28px。

**圖表主題傳播**:`lib/chartTheme.ts:getChartTheme()` 執行期讀 `--up/--down/--chart-* …` 轉 rgb 餵 lightweight-charts;Recharts series 綁 `hsl(var(--chart-N))`。故換 palette **自動重著色所有圖**;元件在主題切換時重呼叫 `getChartTheme()`。

---

## 4. 原語(`components/ui/`)—— 頁面該用這些

- **DataTable**(`table.tsx`):column 驅動、`--row-h` 密度、`mobileMode: "cards"|"scroll"`(≤6 欄有主次→cards;>6 欄或需逐欄比對→scroll+sticky 首欄)、numeric 欄右對齊+`<Num>`(勿 inline toFixe)。a11y props:`caption`(sr-only)、`loading`(aria-busy)、`live`(aria-live tbody,給 tick 表)。
- **StatCard**:`label/value/delta/icon/hint/compact/live`;值 `text-stat`+tabular-nums;`live`→值 aria-live。
- **Card**:`surface="card|1|2"` 挑 ladder 階;base 帶 shadow-highlight;Title `text-heading`、Description `text-data`。
- **PageHeader**:單 `<h1>` `text-title`;`breadcrumb` slot + `id`(供 `aria-labelledby`)。
- **EmptyState**(`role=status`)/ **LoadingState**(`role=status`+aria-live)—— 收斂 ad-hoc 空/載入態。
- **ChartTooltip**(`ChartTooltip.tsx`):Recharts `content` slot 的唯一樣式化 tooltip(border-strong + shadow-popover + mono 值)。用法 `<Tooltip content={<ChartTooltip valueFormatter={(v)=>…} />} />`;`chartAxisTick` 供 XAxis/YAxis `tick`。**16/16 圖已採用**(僅 AllocationPie pie 固定 label 特例保留 inline)。
- `<Num>/<DeltaText>`(`components/Num.tsx`):所有數字的 mono/tabular 來源;`DeltaText` 依 sign 上色(text-up/down/flat)。

---

## 5. Shell / IA

- **AppLayout**:topbar `h-11`(44px 觸控)+ `border-subtle` + `shadow-highlight`;`<main>` 底 padding 引用 `--nav-h`。
- **Sidebar**:desktop rail / mobile drawer;**active item 左 2px `bg-primary` accent bar**;footer 身分區(email + role chip + 登出);4 個 nav group(markets/workspace/ai/system,Finmind 併入 system);eyebrow `text-label`、item `text-data`。
- **BottomNav**(`lg:hidden`):4 tab(Dashboard/Markets/Portfolio/AI)+ More;**active 頂 2px accent bar**;`--nav-h` 高;safe-area padding;44px 觸控。
- **手機三腳導覽**:BottomNav(拇指區高頻)· Drawer(完整 IA,More/hamburger 開)· CommandPalette(⌘K/搜尋跳轉)—— 職責不重疊。

---

## 6. 慣例(新碼遵守)

1. 數字用 `<Num>/<DeltaText>`(勿 inline `toFixed`);表格 numeric 欄同理。
2. 圖表 tooltip 用 `<ChartTooltip>`;軸用 `chartAxisTick`;series 綁 `hsl(var(--chart-N))`。
3. 漲跌色用 `text-up/down/flat`(隨 market-colors 翻轉);status 用 `success/warning/danger`;**禁硬編 red/green/emerald**(ESLint 擋)。
4. 字級用 role token;**禁 `text-[10px]`**(用 `text-micro`,ESLint 擋)。
5. 間距用 `p-gutter/p-page/space-y-stack/space-y-section`。
6. 空/載入態用 `EmptyState`/`LoadingState`;卡用 `Card` 的 `surface` prop 挑階。

---

## 7. 已知剩餘 debt(需部署視覺驗證或逐案判斷,勿盲改)

- **`text-[11px]` → `text-label`**:text-label 帶 0.06em tracking,會撐開 body 文字 → 需逐處分辨 eyebrow(可轉)vs body(不轉)。
- **`toFixed` → `<Num>`**:Num 是 mono 字體;僅「已在 `font-mono` 情境」等價,其餘是可見字體變化 → 逐處看渲染。
- **未 token 化色(blue/purple/cyan/amber/emerald ~60+ 處)**:**大多是 categorical/decorative**(persona 分類色、工具名高亮、結論主題色、相關性 diverging 色),盲映成 success/warning 會**語意錯誤** → 逐案判斷。
- **7 手寫 `<table>`**(OptionsPanel/PersonaLeaderboardCard/HoldingsTable 等):前輪**刻意保留**(有 3 段響應式/client 排序/條件色,二元 mobileMode 會劣化)→ **勿轉 DataTable**。

**這些的正解**:部署 → 目視真頁 → 逐項判斷方向後安全執行,而非在未部署狀態盲改。
