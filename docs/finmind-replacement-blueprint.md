# FinMind 完全取代 + Crypto 資料管線藍圖

> 2026-07-12 規劃定稿。目標:FinMind Sponsor 訂閱(6000 req/hr)到期後不續約,全部 dataset 改自爬;
> 並新增加密貨幣資料管線(Top 200 幣種,納入 finmind clone catalog 對外供應)。

## 決策紀錄

| 議題 | 決定 |
|---|---|
| 最終目標 | 完全取代 FinMind,訂閱到期後不續 |
| 台股自爬優先序 | 先加 per-dataset 用量埋點,收 1–2 週數據後依實際額度消耗排序 |
| Crypto 範圍 | 歷史 OHLCV(日線+小時線)+ 幣種資訊/市值排行 + funding rate/未平倉量 |
| Crypto universe | CoinGecko 市值 Top 200 → 對映 Binance USDT 交易對 |
| Crypto 對外供應 | 納入 finmind clone catalog(CryptoPrice 等 dataset code,走同一套 billing) |
| 連線驗證 | 本機已實測 Binance spot/futures、CoinGecko 皆可達(無地區封鎖) |

## 現況盤點(2026-07-12)

`backend/finmind/` 子系統已具備:catalog ~85 datasets、`dataset_sources.active_source` 免改碼切換、
selfcrawl 架構(`resolve_client()` + per-source `_DISPATCH`)、`dry_run_cutover.py`(row count 比對)、
`backfill_progress` ledger、billing。**注意:CLAUDE.md 的 Phase B 覆蓋數字已落後,以 `supported_datasets()` 為準。**

Dataset 缺口分類:

- **A 類(18)已 wired 可即刻切換**:twse 14(Price/Institutional/Margin/Info/PER/TotalReturnIndex/Shareholding/TotalInst/TotalMargin/DayTrading/BuyBack/Delisting/TradingDate/SecuritiesLending)+ mops 4(MonthRevenue/FinancialStatements/BalanceSheet/CashFlows)
- **B 類(~20)只差 selfcrawl handler**(底層 client 已存在):InfoWithWarrant(+Summary)、Suspended、DayTradingSuspension、PriceLimit、MarginShortSaleSuspension、DailyShortSaleBalances、SecuritiesTraderInfo、TotalExchangeMarginMaintenance、BlockTradingDailyReport、BlockTrade、LoanCollateralBalance、DispositionSecuritiesPeriod、DayTradingBorrowingFeeRate、Dividend(+Result)、CapitalReductionReferencePrice、MarketValueWeight、SplitPrice、ParValueChange
- **C 類(19)要新寫來源模組**:taifex 14(期貨/選擇權全系列)、tpex 4(可轉債)、tdcc 1(HoldingSharesPer 股權分散)
- **D 類(~18)純 FinMind 需另闢來源**:PriceAdj/WeekPrice/MonthPrice(derived 自算)、StatisticsOfOrderBookAndTrade(TWSE MI_5MINS)、GovernmentBankBuySell、MarketValue、BusinessIndicator(國發會)、IndustryChain(靜態 seed)、MACRO 5 個(央行/FRED/CNN)
- **棄用**:realtime/tick 類 ~10 個設計上不落地;TradingDailyReport 系列 FinMind 已 enum-removed(migration 0020),標 deprecated
- **用量埋點缺口**:`finmind_connector.py` Redis counter 為全域無 dataset 維度,成功呼叫無 per-dataset log ⇒ W0 必須先埋

## 波次計畫(~12 週;台股與 crypto 兩條 track 並行)

### W0(週 0–1):用量埋點 + 值比對工具 + 基線
- `data/tw/finmind_connector.py` `_query()` 成功路徑加 `HINCRBY finmind:upstream:usage:{yyyymmdd} {dataset_code}`(TTL 35 天);新增 `finmind/scripts/usage_report.py` 產出 7/14 天排行
- `dry_run_cutover.py` 加 `--values` 值比對模式(見下節);`ingest/mappings/_registry.py` 每 dataset 加 `compare_spec`
- A 類 18 個跑 row count + 值比對基線
- **驗收**:usage report 可產排行;A 類 dry_run ±5% 且值 mismatch < 1%

### W1(週 1–4):A 類 cutover + B 類 handlers
- A 類:先切 5 個低下游用量者觀察 3 天 → 全切(`PATCH /api/finmind/admin/datasets/{code}`)
- B 類:依 W0 用量排行排序;每個 = `selfcrawl/twse.py|mops.py` 加 `_fetch_*()` + `_DISPATCH`,缺的底層函式補進 `data/tw/twse_connector.py`/`mops_connector.py`,缺 mapping 補 `ingest/mappings/`;附 golden-file test
- **驗收**:每 dataset 值比對過 → 切 active_source → 回填 5 年 → 連續 3 個排程日無失敗

### W2(週 4–6):TAIFEX 來源(C 類 14)
- 新 `selfcrawl/taifex.py`,`register_connector('taifex', ...)` 掛入;擴充 `data/tw/taifex_connector.py`(CSV 下載型,Big5 編碼、逐期格式偵測,raw CSV 落地供 replay)
- **驗收**:同 W1;特別驗 `contract_date` 對映 FinMind 格式

### W3(週 6–8):TPEX + TDCC(C 類 5)
- 新 `data/tw/tpex_connector.py`(可轉債 4)、`tdcc_connector.py`(opendata CSV,週五更新);對應 selfcrawl 模組
- **驗收**:同上;HoldingSharesPer 驗 15 級距加總 = 100%(內建不變量)

### W4(週 8–10):D 類 derived + 外部來源
- `selfcrawl/derived.py`(來源 `'derived'`,fetch 讀自家 DB):PriceAdj 由日線+除權息自算、Week/MonthPrice resample
- `selfcrawl/macro.py`:央行匯率利率、FRED 原油/公債殖利率、CNN Fear&Greed、國發會景氣指標;TWSE MI_5MINS 補 StatisticsOfOrderBookAndTrade;IndustryChain 靜態 seed
- realtime/tick + TradingDailyReport 系列 catalog 標 deprecated
- **驗收**:derived 容差放寬 1% + 20 檔 spot check;macro 只驗 coverage(非核心可容忍 gap)

### W5(與 W2–W4 並行):Crypto 子系統(見下節)
- **驗收**:Top200 日線 5 年 + 小時線 2 年回填全綠;排程連跑 7 天無洞(gap-check 預期 bar 數);5 大幣 close 對 CoinGecko 誤差 < 1%

### W6(週 10–12):cutover 演練 + 到期
- 全 catalog 切自爬,雙軌 dry_run 每日跑 1–2 週
- 斷源演練:FinMind quota 設 0 一整個排程日,確認 run_due 全綠且 usage counter = 0(抓偷打 FinMind 的 code path)
- **驗收**:連續 5 個交易日 upstream usage = 0 且 daily ingest 全成功 → 不續訂

## Crypto 子系統設計

### Catalog / 路由
- `primary_source` 值域擴充 `'binance'`/`'coingecko'`,fallback=None(「生來就 self」形態;檢查 runner/admin 有無 `primary_source=='finmind'` 隱含假設)
- 新 dataset:`CryptoPrice`(1d)、`CryptoPriceHourly`、`CryptoInfo`、`CryptoFundingRate`、`CryptoOpenInterest`
- `ingest_freq` 新值 `hourly`/`8h`:改 `scheduler/runner.py` due 判斷 + cron 改每小時呼叫 `run_due`(台股 daily 邏輯加回歸測試)
- `scheduler/dispatcher.py` 加 per-source universe resolver(台股用 tw_stock_info,crypto 用 crypto_universe)

### 新表(`finmind/models/crypto.py` + migration)
| 表 | 要點 |
|---|---|
| `crypto_ohlcv` | PK(symbol, interval, ts),interval='1d'/'1h';hypertable chunk 7d,30 天後壓縮(segmentby symbol,interval) |
| `crypto_universe` | PK coingecko_id;symbol/name/binance_symbol/exchange/status(active/delisted/unmapped)/market_cap_rank |
| `crypto_asset_info` | 週快照 append-only,PK(snapshot_date, coingecko_id) |
| `crypto_funding_rate` | PK(symbol, funding_time) |
| `crypto_open_interest` | hypertable,PK(symbol, ts);⚠️Binance OI 歷史僅 ~30 天,上線日起累積 |

### 來源模組
- `data/crypto/binance_connector.py`(spot klines、futures fundingRate/openInterest、exchangeInfo)、`coingecko_connector.py`(/coins/markets)
- `selfcrawl/binance.py`、`selfcrawl/coingecko.py` 照 twse.py 模式;mappings 加 kline array → OHLCV 欄位

### Universe 管理(weekly job)
CoinGecko top200 → 對映 Binance exchangeInfo(USDT 永續優先+現貨);無對映 = unmapped 只收 info;
下架 → delisted 停排程保留歷史;以 coingecko_id 為穩定主鍵;新進 top200 自動排 backfill。

### Rate limit / 回填估算
- Binance 6000 weight/min(klines weight 2 @ limit 1000):日線 5 年 = 400 calls、小時線 2 年 = 3,600 calls、funding 2 年 = 600 calls ⇒ 以 50% budget 節流**一晚內完成**
- 節流:Redis 滾動 counter + 讀 `X-MBX-USED-WEIGHT-1M` 校正;429/418 指數退避尊重 Retry-After
- CoinGecko free ~30 calls/min:weekly 200 calls 節流 ~8 分鐘

### 排程(UTC)
CryptoPrice 每日 00:10;Hourly 每小時 :05;FundingRate 00:05/08:05/16:05;OI 每小時;universe+asset_info 每週一 01:00

## dry_run_cutover `--values` 值比對設計
- 抽樣:每 dataset 固定 seed 抽 10 symbols(偏大市值 + 2 隨機小型股);無 symbol 維度者全量
- `compare_spec = {key_cols, value_cols: [(col, kind, tol)]}`:價格類相對誤差 ≤0.5%、股數/金額絕對容差、字串 exact
- 判準:key coverage ≥ 98%、per-column mismatch ≤ 1%、輸出前 5 筆 mismatch 明細;超標 exit ≠ 0 可掛 cron

## 風險與緩解
| 風險 | 緩解 |
|---|---|
| TWSE/TPEX 反爬(429/ban) | per-host Redis token bucket(TWSE ≤3 req/5s)、jitter、同 host 序列化、深夜回填錯峰、退避+告警 |
| Binance 地區政策變化 | connector 介面已抽象;備援序 OKX → Kraken;universe.exchange 欄位預留;451/403 告警 |
| TAIFEX CSV 歷史改版 | per-date schema 偵測、golden-file tests、raw CSV 落地 replay、交易日曆跳假日 |
| 訂閱到期時程壓力 | 用量數據驅動優先序;A 類第 1 週即切立減額度消耗;斷源演練提前 2 週;macro 可接受 gap |
| PriceAdj 演算法差異 | 公式文件化、容差 1%、除權息事件日重點驗證 |
| hourly freq 動到共用 due 邏輯 | 台股 daily 回歸測試;crypto 失敗不阻塞台股(per-source try/except) |

## 關鍵檔案
- `backend/data/tw/finmind_connector.py` — W0 埋點(`_query()` 成功路徑)
- `backend/finmind/scripts/dry_run_cutover.py` — `--values` 模式
- `backend/finmind/ingest/selfcrawl/__init__.py` — 新來源註冊點
- `backend/finmind/dataset_catalog.py` — crypto datasets / primary_source 值域 / freq / deprecated
- `backend/finmind/scheduler/dispatcher.py` — hourly/8h due + universe resolver
