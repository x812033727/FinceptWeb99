-- Must run before Alembic migrations
-- TimescaleDB extension + hypertables DDL

CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- OHLCV hypertable (shared US + TW)
CREATE TABLE IF NOT EXISTS ohlcv (
    time        TIMESTAMPTZ  NOT NULL,
    symbol      VARCHAR(20)  NOT NULL,
    market      VARCHAR(4)   NOT NULL,  -- 'US' or 'TW'
    open        NUMERIC(18,6),
    high        NUMERIC(18,6),
    low         NUMERIC(18,6),
    close       NUMERIC(18,6),
    volume      BIGINT,
    interval    VARCHAR(5)   NOT NULL,  -- '1d', '1h', '5m'
    PRIMARY KEY (time, symbol, market, interval)
);

SELECT create_hypertable(
    'ohlcv', 'time',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE
);

ALTER TABLE ohlcv SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, market, interval'
);

SELECT add_compression_policy('ohlcv', INTERVAL '3 months', if_not_exists => TRUE);

-- Taiwan institutional flow hypertable
CREATE TABLE IF NOT EXISTS tw_institutional (
    time        TIMESTAMPTZ NOT NULL,
    symbol      VARCHAR(20) NOT NULL,
    fini_buy    BIGINT,
    fini_sell   BIGINT,
    sitc_buy    BIGINT,
    sitc_sell   BIGINT,
    dealer_buy  BIGINT,
    dealer_sell BIGINT,
    PRIMARY KEY (time, symbol)
);

SELECT create_hypertable(
    'tw_institutional', 'time',
    chunk_time_interval => INTERVAL '3 months',
    if_not_exists => TRUE
);

-- Taiwan margin balance hypertable
CREATE TABLE IF NOT EXISTS tw_margin (
    time             TIMESTAMPTZ NOT NULL,
    symbol           VARCHAR(20) NOT NULL,
    margin_purchase  BIGINT,
    margin_balance   BIGINT,
    short_sale       BIGINT,
    short_balance    BIGINT,
    PRIMARY KEY (time, symbol)
);

SELECT create_hypertable(
    'tw_margin', 'time',
    chunk_time_interval => INTERVAL '3 months',
    if_not_exists => TRUE
);

-- Fundamentals snapshot (JSONB — flexible schema for US + TW)
CREATE TABLE IF NOT EXISTS fundamental_snapshots (
    id          UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    symbol      VARCHAR(20) NOT NULL,
    market      VARCHAR(4)  NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    data        JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fundamentals_lookup
    ON fundamental_snapshots (symbol, market, fetched_at DESC);

CREATE INDEX IF NOT EXISTS idx_fundamentals_gin
    ON fundamental_snapshots USING GIN (data);
