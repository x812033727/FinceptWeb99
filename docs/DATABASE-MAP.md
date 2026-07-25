# FinceptWeb99 資料庫用途地圖

**產出日:** 2026-07-25(唯讀盤點;方法=pg_stat + hypertable 實數 + 全後端程式碼交叉比對)
**規模:** 961 MB;`public` 74 表(720 MB)+ `finmind` schema 78 表(34 MB)+ TimescaleDB chunks(192 MB)

## 怎麼讀這份文件

每張表按「有沒有資料 × 有沒有人讀」分六桶。欄位:實際列數(hypertable 已還原真數)/ 寫入者(排程任務;「—」= 使用者操作經 API 寫入)/ 讀取者 / AI 欄(✅ = 進入每日 AI 討論的情境組裝)。

| 桶 | 數量 | 意義 |
|---|---|---|
| ACTIVE_AI | 26 | 讀寫齊備且進 AI 情境 —— 系統核心 |
| ACTIVE_NO_AI | 24 | 有人讀但只在網頁端,AI 看不到 |
| **UNREAD** | **41** | **有資料、零讀者 —— 純燒儲存的沉睡資產** |
| EMPTY_WITH_WRITER | 36 | 有寫入者卻是空的 —— 斷供或未啟用 |
| FEATURE_EMPTY | 21 | 使用者功能表,還沒有使用者資料(正常) |
| ORPHAN | 2 | 疑似孤兒,需人工覆核後才可談刪除 |

## 三個頭條發現

**1. 整個 `finmind` schema 是「只寫不讀」的平行倉庫。** 每天三班 cron 寫入 1.5-1.7 萬列(期貨/選擇權自營商籌碼、大額交易人、借券、可轉債、產業鏈、外資持股、當沖、處置股…),但全後端**沒有任何一行程式讀它**。UNREAD 41 張裡 finmind 佔 39 張。這是「沒估算到」的最大單一來源:錢已經花了(API 配額、儲存、排程),訊號躺在庫裡。

**2. 斷供表的樣板:`tw_stock_buyback`(庫藏股)。** 有寫入者、0 列、FinMind 422 每日必敗、TWSE 自爬備援存在但不觸發、備用任務被註解。EMPTY_WITH_WRITER 36 張中,finmind 側多數是「dataset 未啟用」(正常),public 側需逐一判定是斷供還是待啟用。

**3. 命名雙胞胎:`fundamental_snapshots`(0 列)vs `fundamentals_snapshots`(7.6 萬列)。** 差一個 s 的遺留表,ORPHAN 覆核首選。

## 後續兩步(已與使用者定案)

- **第二步:按策略對性接入** — 從 UNREAD 挑訊號價值最高者,路由進對應策略的情境(期權籌碼/大額交易人→price_signal;借券/處置→chip_quality;產業鏈→general)。每個接入走完整 spec→plan→SDD。**候選排序見下表。**
- **第三步:清理提案** — ORPHAN 與確認斷供者逐項附證據,使用者逐項裁決;任何刪除單獨授權。

### 第二步候選(按訊號價值 × 資料完整度初排)

| 優先 | 資料 | 表 | 對應策略 | 理由 |
|---|---|---|---|---|
| 1 | 期貨自營商/大額交易人 | `tw_futures_dealer_volume`(26k)、`tw_futures_oi_largetraders` | price_signal | 總經否決放寬後,期貨籌碼正是該策略缺的確認訊號 |
| 2 | 借券餘額/擔保品 | `tw_loan_collateral`(17k)、`tw_securities_lending`、`tw_short_sale_balance_daily` | chip_quality | 空方壓力面,現有籌碼情境完全沒有 |
| 3 | 處置股/暫停交易 | `tw_disposition`、`tw_suspended` | 全策略(風控) | 避免推薦到處置股 —— 直接的風險缺口 |
| 4 | 產業鏈 | `tw_industry_chain`(3.8k) | general | 讓「同族群連動」有真資料可引 |
| 5 | 外資持股比 | `tw_foreign_shareholding`(18k) | chip_quality | 補外資水位維度 |
| 6 | 可轉債 | `tw_cb_daily_overview`(3k) | general | 訊號價值待驗,排最後 |

（crypto 三張表資料量大但與台股日選無關,不列入。）

## 全表清單

### UNREAD — 有資料、零讀者(41)

| 表 | 列數 | 寫入者 | 讀取者 | AI |
|---|---|---|---|---|
| `finmind.crypto_ohlcv` | 40,054 | _registry, _row_transforms … | — | — |
| `finmind.crypto_open_interest` | 32,645 | _registry, _row_transforms … | — | — |
| `finmind.tw_futures_dealer_volume` | 26,385 | _registry | — | — |
| `finmind.tw_foreign_shareholding` | 18,856 | _registry, twse | — | — |
| `finmind.tw_day_trade_daily` | 18,388 | _registry | — | — |
| `finmind.tw_loan_collateral` | 17,534 | _registry, _row_transforms | — | — |
| `finmind.tw_news_articles` | 15,409 | _registry | — | — |
| `finmind.tw_market_value_weight` | 7,110 | _registry | — | — |
| `finmind.crypto_funding_rate` | 6,458 | _registry, _row_transforms … | — | — |
| `finmind.tw_option_dealer_volume` | 5,269 | _registry | — | — |
| `finmind.tw_option_daily` | 4,188 | _registry, taifex | — | — |
| `finmind.tw_industry_chain` | 3,831 | _registry | — | — |
| `finmind.tw_cb_daily_overview` | 3,003 | _registry | — | — |
| `finmind.tw_market_value` | 2,734 | _registry | — | — |
| `finmind.crypto_asset_info` | 2,602 | _registry, _row_transforms … | — | — |
| `finmind.tw_short_sale_balance_daily` | 2,215 | _registry | — | — |
| `finmind.tw_stock_per_daily` | 2,015 | _registry | — | — |
| `finmind.tw_futopt_master` | 1,308 | _registry | — | — |
| `finmind.tw_block_trade` | 1,152 | _batch_transforms, _registry | — | — |
| `finmind.tw_futures_oi_largetraders` | 682 | _registry | — | — |
| `finmind.tw_securities_lending` | 448 | _registry, _row_transforms … | — | — |
| `finmind.tw_futures_daily` | 385 | _registry, taifex | — | — |
| `finmind.tw_cb_daily` | 375 | _batch_transforms, _registry | — | — |
| `finmind.tw_short_sale_suspension` | 375 | _registry | — | — |
| `finmind.tw_cb_inst_daily` | 164 | _registry | — | — |
| `finmind.tw_option_oi_largetraders` | 148 | _registry | — | — |
| `finmind.tw_futures_spread` | 114 | _registry | — | — |
| `finmind.tw_disposition` | 52 | _registry | — | — |
| `finmind.tw_futures_inst_daily` | 40 | _registry | — | — |
| `finmind.tw_exchange_rate` | 38 | _registry | — | — |
| `finmind.tw_cb_info` | 36 | _registry | — | — |
| `finmind.tw_day_trade_fee` | 20 | _registry | — | — |
| `finmind.tw_broker_master` | 12 | _batch_transforms, _registry … | — | — |
| `finmind.tw_margin_maintenance` | 12 | _registry | — | — |
| `finmind.tw_option_inst_daily` | 12 | _batch_transforms, _registry | — | — |
| `finmind.tw_total_inst_daily` | 12 | _registry | — | — |
| `finmind.tw_total_margin_daily` | 12 | _registry | — | — |
| `finmind.us_fear_greed` | 9 | _registry | — | — |
| `finmind.commodity_price` | 6 | _registry, fred | — | — |
| `finmind.tw_business_indicator` | 1 | _registry | — | — |
| `finmind.tw_option_settlement` | 1 | _registry, _row_transforms | — | — |

### EMPTY_WITH_WRITER — 有寫入者卻是空的(36)

| 表 | 列數 | 寫入者 | 讀取者 | AI |
|---|---|---|---|---|
| `finmind.api_usage_events` | 0 | diagnostic | config_status, keys | — |
| `finmind.macro_interest_rate` | 0 | _registry | — | — |
| `finmind.subscriptions` | 0 | scheduler | keys, manager, plans …+3 | — |
| `finmind.tw_balance_sheet` | 0 | _registry | tw_factor_service | — |
| `finmind.tw_broker_daily_report` | 0 | _registry, _row_transforms | — | — |
| `finmind.tw_buyback` | 0 | _registry | chip | ✅ |
| `finmind.tw_capital_reduction` | 0 | _registry | — | — |
| `finmind.tw_cash_flow` | 0 | _registry | tw_factor_service | — |
| `finmind.tw_dividend` | 0 | _registry | — | — |
| `finmind.tw_dividend_result` | 0 | _registry | — | — |
| `finmind.tw_futures_settlement` | 0 | _registry | — | — |
| `finmind.tw_holdings_aggregates` | 0 | _registry, ingest_holdings_aggregates_tw | tw_chip | — |
| `finmind.tw_income_statement` | 0 | _registry | tw_factor_service | — |
| `finmind.tw_par_value_change` | 0 | _registry | — | — |
| `finmind.tw_revenue_monthly` | 0 | _registry, auto_run_discussion … | chip, focus_briefs, tw_fundamentals …+1 | ✅ |
| `finmind.tw_split` | 0 | _registry | — | — |
| `finmind.tw_stock_minute` | 0 | _registry, _row_transforms | — | — |
| `finmind.tw_stock_tick` | 0 | _batch_transforms, _registry | — | — |
| `finmind.tw_total_return_index` | 0 | _registry | — | — |
| `finmind.us_bond_yield` | 0 | _registry, fred | — | — |
| `public.alert_events` | 0 | alert_streaks_tw, daily_alert_digest … | alert_rules, alert_service | — |
| `public.discussion_strategy_templates` | 0 | monitor_strategy_health, scheduler | confidence_calibrator, loop, persona_performance_service …+10 | ✅ |
| `public.holdings` | 0 | _registry, _row_transforms … | context_assembly, finmind_chain_service, paper_risk_service …+17 | ✅ |
| `public.investment_theses` | 0 | sync_thesis_events | alert_service, router, weekly_research_summary_service | — |
| `public.notification_channels` | 0 | daily_alert_digest | channel_notification_service, router, schemas | — |
| `public.ohlcv` | 0 | _registry, _row_transforms … | alert_service, backtest_sweep_service, builder …+30 | ✅ |
| `public.paper_orders` | 0 | paper_order_matching, scheduler | paper_matching_service, paper_performance_service, paper_risk_service …+2 | — |
| `public.portfolio_snapshots` | 0 | portfolio_snapshot, scheduler | portfolio_analytics, portfolio_service, router …+2 | — |
| `public.portfolios` | 0 | ingest_taiex_tr_history, portfolio_snapshot | context_assembly, portfolio_cash_service, portfolio_review …+7 | ✅ |
| `public.quote_snapshots` | 0 | ingest_quotes_retention_tw, scheduler … | intraday_service, quotes, router | — |
| `public.stock_reports` | 0 | generate_daily_picks | daily_pick_service, router, stock_report …+2 | — |
| `public.strategy_health_metrics` | 0 | monitor_strategy_health, scheduler | daily_scoreboard_service, strategies, strategy_comparison_service …+2 | — |
| `public.thesis_events` | 0 | scheduler, sync_thesis_events | alert_service, router, schemas …+1 | — |
| `public.tw_institutional` | 0 | _registry, alert_streaks_tw … | chip, tw_market_service | ✅ |
| `public.tw_margin` | 0 | _registry, ingest_margin_tw | chip, tw_market_service | ✅ |
| `public.tw_stock_buyback` | 0 | ingest_buyback_tw | tw_fundamentals | — |

### FEATURE_EMPTY — 使用者功能表(尚無資料,正常)(21)

| 表 | 列數 | 寫入者 | 讀取者 | AI |
|---|---|---|---|---|
| `finmind.api_keys` | 0 | — | keys, router, schemas | — |
| `public.api_keys` | 0 | — | keys, router, schemas | — |
| `public.auth_invitations` | 0 | — | router, users | — |
| `public.backtest_runs` | 0 | — | backtest_run_service, router, schemas | — |
| `public.chart_drawings` | 0 | — | router | — |
| `public.data_quality_feedback` | 0 | — | router | — |
| `public.paper_fills` | 0 | — | paper_matching_service, paper_performance_service, paper_risk_service …+3 | — |
| `public.paper_risk_policies` | 0 | — | paper_risk_service, paper_trading | — |
| `public.password_reset_tokens` | 0 | — | router | — |
| `public.portfolio_cash_entries` | 0 | — | paper_risk_service, paper_trading_service, portfolio_attribution_service …+1 | — |
| `public.portfolio_transaction_imports` | 0 | — | portfolio_service | — |
| `public.stock_pick_runs` | 0 | — | daily_pick_service, router, schemas …+1 | — |
| `public.strategy_versions` | 0 | — | persona_performance_service, persona_status_service, strategies …+3 | — |
| `public.transactions` | 0 | — | portfolio_attribution_service, portfolio_cash_service, portfolio_service …+3 | — |
| `public.tw_factor_model_versions` | 0 | — | tw_factor_registry_service | — |
| `public.tw_factor_research_runs` | 0 | — | tw_factor_registry_service | — |
| `public.user_llm_provider_keys` | 0 | — | llm_key_service | — |
| `public.watchlist_items` | 0 | — | context_assembly, router, schemas …+1 | ✅ |
| `public.watchlists` | 0 | — | context_assembly, router, schemas …+2 | ✅ |

| `public.price_alerts` | 0 | alert_service/web_push(手動覆核) | — | — |
| `public.push_subscriptions` | 0 | alert_service/web_push(手動覆核) | — | — |

### ORPHAN — 疑似孤兒(刪除前需人工覆核)(2)

| 表 | 列數 | 寫入者 | 讀取者 | AI |
|---|---|---|---|---|
| `finmind.payment_events` | 0 | — | — | — |
| `public.fundamental_snapshots` | 0 | — | — | — |

### ACTIVE_NO_AI — 活躍但 AI 看不到(24)

| 表 | 列數 | 寫入者 | 讀取者 | AI |
|---|---|---|---|---|
| `public.news_articles` | 227,068 | _registry, _row_transforms … | news, news_backfill_service, news_fulltext …+3 | — |
| `finmind.tw_stock_info` | 65,470 | _registry, _row_transforms … | config_status, datasets, finmind_chain_service …+1 | — |
| `finmind.tw_price_limit_daily` | 28,050 | _registry | tw_factor_service | — |
| `public.tw_company_classification_snapshots` | 15,092 | — | tw_factor_service, tw_symbol_service | — |
| `public.tw_security_master_versions` | 15,092 | — | tw_factor_service, tw_security_master_service | — |
| `finmind.tw_stock_price_adj` | 8,954 | _registry | tw_factor_service | — |
| `finmind.backfill_progress` | 8,594 | __init__, _types … | datasets, finmind_chain_service | — |
| `public.ingest_health_history` | 4,542 | — | infra, ingest_health, schemas | — |
| `public.tw_company_info` | 1,374 | — | tw_factor_service, tw_security_master_service, tw_symbol_service | — |
| `public.tw_stock_futures_oi` | 1,320 | ingest_stock_futures_oi_tw | quotes | — |
| `public.audit_events` | 897 | — | router | — |
| `public.tw_stock_disposition` | 600 | — | tw_fundamentals | — |
| `public.signal_audit_history` | 493 | scheduler, snapshot_signal_audit | schemas, signal_audit, signal_audit_service | — |
| `public.tw_stock_suspended` | 484 | — | tw_fundamentals | — |
| `finmind.tw_suspended` | 429 | _registry | tw_factor_service | — |
| `finmind.dataset_sources` | 90 | __init__, backfill … | config_status, datasets, finmind_proxy | — |
| `public.decision_journal_entries` | 23 | — | daily_pick_service, decision_journal_service, weekly_research_summary_service | — |
| `public.user_consents` | 6 | — | router | — |
| `finmind.tw_delisting` | 3 | _registry, backfill | tw_factor_service | — |
| `public.market_provider_keys` | 3 | — | market_key_service | — |
| `public.system_task_configs` | 3 | — | schemas, system_task_config_service, system_tasks | — |
| `finmind.plans` | 1 | __init__, _registry … | __init__, _shared, finmind_proxy …+8 | — |
| `public.llm_provider_keys` | 1 | — | lesson_embedding_service, llm_key_service | — |
| `public.runtime_settings` | 1 | — | router, runtime_config_service, runtime_settings …+1 | — |

### ACTIVE_AI — 活躍且進 AI 情境(26)

| 表 | 列數 | 寫入者 | 讀取者 | AI |
|---|---|---|---|---|
| `public.tw_stock_shareholding` | 2,642,600 | ingest_holdings_aggregates_tw | technical, tw_chip | ✅ |
| `public.tw_stock_day_trading_daily` | 488,724 | ingest_risk_signals_tw | technical, tw_fundamentals | ✅ |
| `public.ohlcv_daily` | 334,022 | _registry, auto_run_discussion … | alert_service, backtest_sweep_service, builder …+24 | ✅ |
| `public.tw_institutional_daily` | 318,501 | _registry, alert_streaks_tw … | chip, tw_chip, tw_market_service | ✅ |
| `public.tw_margin_daily` | 101,746 | _registry, ingest_margin_tw | chip, tw_chip, tw_fundamentals …+1 | ✅ |
| `public.fundamentals_snapshots` | 76,425 | auto_run_discussion, ingest_fundamentals_tw | focus_briefs, tw_factor_service, tw_fundamentals …+1 | ✅ |
| `public.tw_revenue_monthly` | 67,638 | _registry, auto_run_discussion … | chip, focus_briefs, tw_fundamentals …+1 | ✅ |
| `finmind.ohlcv_daily` | 45,292 | _registry, auto_run_discussion … | alert_service, backtest_sweep_service, builder …+24 | ✅ |
| `finmind.tw_institutional_daily` | 18,467 | _registry, alert_streaks_tw … | chip, tw_chip, tw_market_service | ✅ |
| `public.llm_usage_events` | 5,862 | — | contexts, llm_usage_service, usage_breakdown | ✅ |
| `public.discussion_turns` | 4,639 | auto_run_discussion, prune_discussion_contexts | consensus, contexts, discussion_service …+13 | ✅ |
| `finmind.tw_margin_daily` | 2,194 | _registry, ingest_margin_tw | chip, tw_chip, tw_fundamentals …+1 | ✅ |
| `public.tw_govt_bank_flow_daily` | 1,982 | ingest_govt_bank_flow_tw | chip, tw_chip | ✅ |
| `public.corporate_announcements` | 1,293 | ingest_announcements_tw, ingest_announcements_us … | announcements, builder, focus_briefs …+4 | ✅ |
| `public.discussion_lessons` | 1,086 | — | discussion_lesson_service, lesson_embedding_service, lesson_tier_service …+3 | ✅ |
| `public.discussion_round_contexts` | 585 | prune_discussion_contexts, scheduler | discussion_service, lesson_tier_service, round_digest …+2 | ✅ |
| `finmind.crypto_universe` | 208 | coingecko, crypto_universe_refresh … | discussion_service, focus_briefs, symbols | ✅ |
| `public.discussions` | 118 | auto_run_discussion, monitor_strategy_health … | __init__, _helpers, announcements …+67 | ✅ |
| `public.tw_market_institutional_daily` | 100 | — | chip, tw_chip | ✅ |
| `public.tw_vix_daily` | 78 | ingest_tw_vix | builder, quotes, regime_classifier …+1 | ✅ |
| `finmind.tw_trading_calendar` | 63 | _registry, auto_run_discussion … | builder, daily_scoreboard_service, decision_journal_service …+5 | ✅ |
| `public.persona_overrides` | 24 | — | loop, persona_config, persona_override_service …+2 | ✅ |
| `finmind.tw_govt_bank_flow` | 12 | _batch_transforms, _registry … | chip, tw_chip | ✅ |
| `public.users` | 3 | auto_run_discussion, daily_alert_digest … | context_assembly, daily_stock_strategies, db_browser …+14 | ✅ |
| `public.discussion_auto_run_configs` | 2 | auto_run_discussion | discussion_auto_run_config_service, email_service | ✅ |
| `public.backtest_sweeps` | 1 | — | _helpers, backtest_sweep_service, confidence_calibrator …+13 | ✅ |

