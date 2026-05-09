"""Single source of truth for Redis cache TTLs across the market services.

Each market service used to declare its own ``TTL_QUOTE``,
``TTL_HISTORY``, ``TTL_FUNDAMENTALS``, ``TTL_NEWS``, etc. — sometimes
mid-file, sometimes at the bottom near the relevant function. Tuning
became a hunt-and-replace exercise; updating one and forgetting another
quietly diverged. Centralizing them here keeps ops tuning to a single
file edit + restart.

Constants are grouped by data shape, then by market where the cadence
genuinely differs (TW quotes lag 3–5 min anyway → 60s OK; US live →
15s; crypto Kraken WS → 10s). Values come from production-tuned
defaults; do not change without checking the corresponding live cron
cadence.
"""

# ── quote (live price + change) ──────────────────────────────────
TTL_QUOTE_TW = 60        # TWSE OpenAPI is 3–5 min delayed; 1 min is plenty
TTL_QUOTE_US = 15        # Polygon is ~real-time; 15 s mirrors WS pump cadence
TTL_QUOTE_CRYPTO = 10    # Kraken WS push refreshes faster than the cache
TTL_QUOTE_US_OFF_HOURS = 5 * 60  # overseas indicator snapshots cached during
                                  # off-hours; US indices barely move when US
                                  # is closed but TW session is live.

# ── history (daily OHLCV bars) ───────────────────────────────────
TTL_HISTORY_DAILY = 4 * 3600        # 4 h — yesterday's bars don't change
TTL_HISTORY_INTRADAY = 5 * 60       # 5 min — used by the crypto service

# ── fundamentals (PE/PB/yield, snapshots) ────────────────────────
TTL_FUNDAMENTALS = 24 * 3600        # daily refresh (TWSE BWIBBU_d / yfinance)
TTL_VALUATION_BAND = 24 * 3600
TTL_DIVIDENDS = 24 * 3600
TTL_EARNINGS = 6 * 3600              # next-earnings date refresh — yfinance
                                     # calendar; 4 ticks per day is plenty,
                                     # the date itself only flips weekly

# ── derived TW-specific ──────────────────────────────────────────
TTL_INSTITUTIONAL = 4 * 3600        # 法人買賣超 — once per trading day
TTL_MARGIN = 4 * 3600                # 融資融券 — once per trading day
TTL_REVENUE = 12 * 3600              # 月營收 — monthly publication
TTL_DERIVATIVES = 4 * 3600          # TAIFEX 三大法人 / 個股期 OI — daily post-close

# ── screener / news / options ────────────────────────────────────
TTL_SCREENER = 10 * 60               # 10 min — heavy compute upstream
TTL_SCREENER_CRYPTO = 60             # crypto universe is 20 symbols, cheaper
TTL_NEWS = 5 * 60                    # 5 min — Google News RSS / connector cap
TTL_OPTIONS = 5 * 60                 # 5 min — chain shifts only with quote

# ── FX (TWD/USD via FRED DEXTW) ──────────────────────────────────
TTL_FX = 4 * 3600
TTL_FX_LAST_KNOWN = 30 * 86400       # 30 days — used as a fallback floor
TTL_FX_HISTORICAL = 90 * 86400       # 90 days — historical rates are immutable;
                                     # long TTL lets old portfolios reload fast
