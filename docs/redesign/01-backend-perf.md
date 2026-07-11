# 後端架構與效能藍圖

> 本文件是[架構重規劃藍圖](00-overview.md)的後端章節。原則:**保留 FastAPI + PG(Timescale)+ Redis,不引入新中介軟體(無 Celery / Kafka)**。演進方向是「把已存在的 Redis 協調原語用足,把 runtime 拓撲從『2 個什麼都做的 worker』改成『N 個純服務 worker + 1 個排程 worker』」。

## 1. 現況診斷

### 1.1 誠實的優勢(不要在重構中破壞這些)

- **快取紀律極佳**:`backend/cache/cache_ttls.py` 單一 TTL 來源、`cache_set_json_unless_empty` 的「暫時性空值不快取」政策、Prometheus `cache_hits_total/cache_misses_total` 已內建於 `cache_get_json`(`backend/cache/redis_cache.py:49-79`)。
- **刻意的非抽象化有文件背書**:`docs/architecture-notes.md` 明確記錄了不做泛用 `httpx` wrapper、不做 `cached_fetch(key, fn, ttl)` 的理由,且理由成立 —— 本藍圖**不挑戰**這兩項決策。
- **跨 process 協調原語已齊備**:Redis Lua token bucket(`backend/cache/redis_cache.py:170-224`)、`acquire_lock` SET-NX(`backend/cache/redis_cache.py:227`),20/34 個排程任務已使用 Redis lock。
- **降級路徑完整**:US 四層 waterfall、TW 三層 + DB archive、FinMind IP-ban circuit breaker、don't-cache-empty。
- **Timescale hypertable 已有實測方法論**(`docs/perf.md`,p95 < 50ms 驗收標準)。

### 1.2 弱點與風險(依嚴重度排序,附證據)

| # | 問題 | 證據 | 影響 |
|---|------|------|------|
| W1 | **排程器在每個 uvicorn worker 各跑一份**。lifespan 對每個 worker process 執行,`--workers 2`(`backend/Dockerfile:35`、`docker-compose.yml:83`)意味 34 個 job × 2 份。`max_instances=1` 只在單 process 內生效 | `backend/main.py:154-160` + `backend/tasks/scheduler.py:9` | 熱路徑任務 `refresh_us_quotes`(10s)、`refresh_tw_quotes`(60s)、`refresh_crypto_quotes`(30s)、`refresh_us_screener`(5min)**均無 Redis lock**(grep 證實),上游 API 呼叫全部雙倍,Polygon/TWSE 配額燒兩倍 |
| W2 | **KrakenTickerPump 也跑兩份**(`backend/main.py:203-205`),每個 tick 被 publish 到 `market:updates` 兩次;兩個 worker 的 pubsub listener 各自 dispatch,delta suppression(<0.01%)只擋住第二次的 client send,CPU 與 Redis 流量仍是雙倍 | `backend/main.py:203`、`backend/api/websocket/manager.py:248-252` | WS 管線吞吐量減半、雙倍 JSON 反序列化 |
| W3 | **排程器與請求服務共用同一 event loop**:`refresh_us_screener` 每 5 分鐘走完整 waterfall,含 Stooq「~20 秒逐一序列爬行」(`backend/tasks/scheduler.py:59-66` 註解自述),期間的 JSON 解析 / 建構是純 CPU,直接推高同 worker 的請求延遲尾巴 | `backend/tasks/scheduler.py:55-66` | p95/p99 延遲週期性抖動 |
| W4 | **`_pubsub_listener` 沒有重連機制**:`_listen_loop` 的 `async for` 在 Redis 連線中斷時直接結束,task 靜默死亡,之後所有 WS delta 停止直到重啟 —— `finally` 只做 unsubscribe | `backend/api/websocket/manager.py:197-211` | Redis 瞬斷 = WS 永久靜音,無任何告警 |
| W5 | **WS dispatch 是 O(connections) 線性掃描 + 序列 send**:每則 pubsub 訊息走遍所有連線比對 `sub_key`,且 `_safe_send` 直接 await `ws.send_text` —— 一個慢 client 拖慢整條 dispatch;payload 對每個訂閱者重複 `json.dumps` | `backend/api/websocket/manager.py:224-241` | Kraken 次秒級 tick × 20 symbols 下,連線數成長時 fan-out 成本平方級上升 |
| W6 | **snapshot 逐 key 序列讀 Redis**:`_send_snapshot` 對每個訂閱 symbol 各發一次 `cache_get`,20 個 symbol = 20 次 RTT | `backend/api/websocket/manager.py:149-163` | 訂閱瞬間延遲;應改 `MGET` |
| W7 | **無 HTTP 壓縮、無 ETag**:`backend/main.py` 無 `GZipMiddleware`,`docker/nginx/nginx.conf` 亦無 `gzip on`(grep 證實)。screener / history / discussion contexts 是大 JSON | `backend/main.py:215-236`、`docker/nginx/nginx.conf` | 頻寬與 TTFB 白白浪費 5-10× |
| W8 | **FinMind auto-init 遷移競態**:`FINMIND_AUTO_INIT=true` 預設下每個 pod 都跑 `alembic upgrade head`,且自 PR #313 起目標是**主庫**的 `finmind` schema —— 半途 OOM = 主庫 schema 半套用 | `backend/main.py:48-116`、CLAUDE.md「Multi-pod deploys MUST set FINMIND_AUTO_INIT=false」 | 水平擴展的硬阻礙;compose 的 `migrate` service 只跑主庫 alembic(`docker-compose.yml:71`),不含 finmind |
| W9 | **DB 連線池未按拓撲計算**:`pool_size=10, max_overflow=20`(`backend/db/session.py:8`)× 2 workers = 尖峰 60 條,再加 finmind engine 與排程任務共搶;31GB 共用主機上的 PG `max_connections` 沒有對應預算 | `backend/db/session.py:6-10` | 尖峰時 pool exhaustion 表現為隨機慢請求 |
| W10 | **可觀測性斷線**:middleware 與 counter 都在(`backend/middleware/metrics.py`、`PrometheusMiddleware`),但 fincept99 部署跳過 prometheus/grafana 容器(`127.0.0.1:9090` 寫死,與共用主機上其他服務衝突),cache hit-rate、latency histogram 全部有產出、無人收 | `docker-compose.yml:160-189` | 上述所有問題目前都不可量測 |
| W11 | **索引覆蓋未經審計**:模型層有 51 處 index 定義,熱表如 `ohlcv_daily(market,symbol,ts)`、`news_articles` 有複合索引,但 discussion 系(owner-scoped 查詢)、`llm_usage_events`、`corporate_announcements` 等新表未經 `pg_stat_statements` 驗證;主庫 Timescale 壓縮預設關閉(CLAUDE.md 明載) | `backend/models/*.py` | 慢查詢無證據、無基準 |
| W12 | **外部 API waterfall 尾延遲**:未命中快取的請求可能序列穿越 Polygon → yfinance → Stooq → Finnhub 四層,每層各自 timeout;watchlist enrichment 類的多 symbol 請求若逐一 await 會疊加 | CLAUDE.md waterfall 章節、`backend/tasks/scheduler.py:55-66` | 冷快取請求 p99 可達數十秒(靠 screener warm cron 掩蓋,而 cron 本身又是 W3) |

## 2. 目標架構

### 2.1 排程器獨立容器(核心建議,尺寸 M)

新增 compose service `scheduler`,同一個 image,入口改為 `python -m worker`(新檔,啟動 scheduler + KrakenTickerPump + 各 warmup,不啟動 uvicorn);backend 的 lifespan 以 `SCHEDULER_ENABLED=false`(新 env,預設 true 保持單容器開發體驗)跳過 `setup_jobs()/scheduler.start()` 與 pump。

- **必要的解耦**:`register_push_impl(push_alert_to_user)`(`backend/main.py:158`)目前讓排程任務直接推 in-process WS。分離後告警需改走 Redis pub/sub(新 channel `user:alerts:{user_id}` 或共用 channel 帶 user_id),web worker 的 listener 負責最後一哩 —— 這是本項的主要工作量。
- **收益**:W1/W2/W3 一次解決;quote 任務不再需要逐一補 Redis lock;web worker 數量可獨立調整;screener warm 的 20 秒 CPU 徹底離開請求路徑。
- **權衡**:多一個容器(~300-500MB RAM,31GB 主機可承受);開發模式需保持單 process 全功能。
- **過渡替代案**(若想先出小 PR,尺寸 S):Redis lock leader election —— lifespan 內 `acquire_lock("scheduler:leader", ttl=90)` + 續租,只有 leader 跑 scheduler/pump。成本低但不解 W3,建議僅作為 Phase 2 前的止血。

### 2.2 In-process 快取:維持現狀(尺寸 S)

`sp500_universe._cache`、TW symbol map、`_ohlcv_latest_cache` 的 per-worker 重複是 `docs/architecture-notes.md` 明載的刻意取捨(cold-start 各付一次 Wikipedia GET,可接受)。**不搬 Redis**。唯一調整:排程器分離後,scheduler 容器的 refresh 寫不到 web worker 的 module cache —— 但現有三層冷啟鏈(Redis snapshot → DB → upstream,`backend/main.py:184-187`)已覆蓋此情境,只需確認 scheduler 的 `refresh_symbol_map` 會回寫 Redis snapshot(現況已是),web worker 靠 TTL 自然收斂。同理,`cached_fetch` 裝飾器**維持不做** —— cache 指標已在 `cache_get_json` 集中埋點,裝飾器能省的兩行換不回 6 個站點各異的 parse/dump/negative-cache 語意。

### 2.3 HTTP 回應壓縮 + ETag(尺寸 S)

- nginx 層加 `gzip on; gzip_types application/json; gzip_min_length 1024;`(改 `docker/nginx/nginx.conf`,不動 Python,SSE/WS 不受影響因 content-type 不匹配)。優於 app 層 `GZipMiddleware`(省 Python CPU,且 middleware 會干擾 `StreamingResponse` buffer)。
- ETag 只加在少數大而穩定的 GET(screener、history、discussion contexts):以回應體 hash 生成 weak ETag 的輕量 middleware,或 per-route 手工處理。收益中等,排在壓縮之後。

### 2.4 Uvicorn / event loop(尺寸 S)

`uvicorn[standard]` 已含 uvloop 且 uvicorn 會自動選用 —— 只需在啟動 log 驗證。排程器分離後,web worker 數從 2 提到 3-4(31GB 主機、CPU 核數允許時),因為不再有「每加一個 worker 就多一份排程器」的懲罰。考慮補 `--limit-concurrency` 與 `timeout-keep-alive` 調整。ProcessPoolExecutor(2) 維持現狀(lazy 建立是 architecture-notes 明載決策)。

### 2.5 DB 連線池與索引審計(尺寸 M)

- 池預算公式化:`(web_workers × (pool_size + max_overflow)) + scheduler_pool ≤ PG max_connections − 保留`。建議 web 每 worker `pool_size=10, max_overflow=10`,scheduler 容器獨立 `pool_size=5, max_overflow=5`,並將四個數字提為 env(`DB_POOL_SIZE` 等,`backend/db/session.py` 一處改動)。
- 索引審計方法:啟用 `pg_stat_statements`(compose 的 postgres command 加 `-c shared_preload_libraries=pg_stat_statements`)→ 跑一週 → 以 `mean_exec_time × calls` 排序前 20 → 對每條 `EXPLAIN (ANALYZE, BUFFERS)` → 補複合索引或改寫。優先懷疑對象:discussion 系 owner-scoped 列表查詢、`llm_usage_events` 聚合、`corporate_announcements` 的 sentiment 掃描。同時評估主庫 `ohlcv_daily` 開 Timescale 壓縮(FinMind 子系統已實測 ~22×)。

### 2.6 WebSocket 管線(尺寸 M)

1. **listener 重連**(S,最高優先):`_listen_loop` 外包 `while True: try/except + backoff`,並加 Prometheus counter `ws_pubsub_reconnects_total`。
2. **反向索引**:`_symbol_subs: dict[str, set[WebSocket]]`,dispatch 從 O(N connections) 降為 O(subscribers of symbol);subscribe/unsubscribe/清理時同步維護。
3. **序列化一次**:delta payload 在 dispatch 前 `json.dumps` 一次,`_safe_send` 收 str。
4. **per-connection send queue**:每連線一個 bounded `asyncio.Queue(64)` + writer task,dispatch 只做 `put_nowait`(滿了丟最舊 delta —— 行情資料丟舊保新是正確語意),慢 client 不再拖累全體。
5. **snapshot 改 MGET**(S):`_send_snapshot` 一次 RTT。

### 2.7 可觀測性(尺寸 S)

prometheus port 已綁 `127.0.0.1:9090` 寫死 —— 改為 `127.0.0.1:${PROMETHEUS_PORT:-9090}:9090`(grafana 已有 `GRAFANA_PORT` 前例,`docker-compose.yml:186`),fincept99 部署設 `PROMETHEUS_PORT=19090` 即可重啟用,零程式碼改動。指標面補三個缺口:WS 連線數 gauge、pubsub dispatch latency histogram、scheduler job 執行時長(APScheduler listener 一個 hook 即可)。

### 2.8 外部 API waterfall(尺寸 S-M)

不做泛用 wrapper(尊重 architecture-notes)。針對性改善:(a) 多 symbol enrichment 路徑確保 `asyncio.gather` 並發而非逐一 await(逐檔案確認 watchlist/portfolio 服務);(b) 每層 waterfall 加 per-tier timeout 預算,確保未命中快取的請求總延遲有上界;(c) 排程器分離後 Stooq 慢爬行天然離開請求 loop,不需再優化。

## 3. 遷移路線圖

每個 Phase = 可獨立合併的 PR 批次;**每批的驗證閘門都包含全量測試套件(~3000 tests)綠燈**。

| Phase | 內容 | 尺寸 | 驗證閘門 |
|---|---|---|---|
| **P0 觀測基線** | prometheus port 參數化重啟用;pg_stat_statements;WS/scheduler 指標補洞 | S | 測試綠;grafana 看得到 cache hit-rate 與 p95;記錄基線數字供後續 Phase 對照 |
| **P1 止血批** | pubsub listener 重連(W4);snapshot MGET(W6);nginx gzip(W7);quote 任務補 Redis lock 作為 leader-election 前的雙保險(W1 減害);FinMind:compose `migrate` service 併跑 `python -m finmind.scripts.init_db`、文件預設多 pod `FINMIND_AUTO_INIT=false`(W8) | S | 測試綠;手動 kill Redis 驗證 listener 復活;`curl -H 'Accept-Encoding: gzip'` 驗證壓縮;雙 worker 下 grafana 確認上游呼叫量減半 |
| **P2 排程器分離** | 新 `worker.py` 入口 + compose `scheduler` service + `SCHEDULER_ENABLED` gate;alert push 改走 Redis pub/sub;Kraken pump 隨遷 | M | 測試綠(alert push 路徑需新測試);部署後驗證:web worker log 無 scheduler 啟動、admin heartbeat 正常、alert 能送達 WS client;p95 抖動對照 P0 基線 |
| **P3 WS 管線重構** | 反向索引、序列化一次、send queue(2.6 之 2-4) | M | 測試綠 + `test_websocket_manager` 擴充;負載測試(50 連線 × 20 symbols)對照 dispatch latency |
| **P4 DB 調優** | 池參數 env 化 + 按拓撲設值;索引審計(pg_stat_statements 一週資料)→ 索引 migration 批次;評估 `ohlcv_daily` Timescale 壓縮 | M | 測試綠;每條新索引附 EXPLAIN 前後對照;`docs/perf.md` 方法論複跑 portfolio 查詢 p95 < 50ms 不退化 |
| **P5 錦上添花** | ETag on 大 GET;waterfall per-tier timeout 預算;web worker 數 2→3-4 | S | 測試綠;304 命中率入 grafana;冷快取 p99 有上界 |

**排序理由**:P0 先行使一切後續改動可量測;P1 全是低風險小 PR 且修掉最危險的靜默故障(W4/W8);P2 是結構性核心,依賴 P1 的 lock 雙保險做安全網;P3/P4 各自獨立、可並行。

## 4. 關鍵實作檔案

- `backend/main.py`(lifespan 拆分、`SCHEDULER_ENABLED` gate、alert push 解耦)
- `backend/api/websocket/manager.py`(listener 重連、反向索引、send queue、MGET)
- `backend/tasks/scheduler.py`(移入新 worker 入口、job lock 補齊)
- `backend/db/session.py`(池參數 env 化)
- `docker-compose.yml`(scheduler service、prometheus port 參數化、finmind migrate、nginx gzip 掛載)
