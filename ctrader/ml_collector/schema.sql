-- Glitch ML Collector v2 — PostgreSQL Schema
-- Run: sudo -u postgres psql -d glitch_ml -f /opt/glitchexecutor/ml_collector/schema.sql

-- OHLCV bars
CREATE TABLE IF NOT EXISTS ml_bars (
    id          BIGSERIAL PRIMARY KEY,
    symbol      VARCHAR(20) NOT NULL,
    timeframe   VARCHAR(4)  NOT NULL,
    bar_time    TIMESTAMPTZ NOT NULL,
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      DOUBLE PRECISION NOT NULL DEFAULT 0,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_bars UNIQUE (symbol, timeframe, bar_time)
);
CREATE INDEX IF NOT EXISTS idx_bars_sym_tf_time ON ml_bars (symbol, timeframe, bar_time DESC);

-- Model signals
CREATE TABLE IF NOT EXISTS ml_signals (
    id              BIGSERIAL PRIMARY KEY,
    signal_id       UUID DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    bot_name        VARCHAR(20) NOT NULL,
    model_name      VARCHAR(40) NOT NULL,
    symbol          VARCHAR(20) NOT NULL,
    timeframe       VARCHAR(4)  NOT NULL,
    account_id      BIGINT NOT NULL,
    vote            VARCHAR(4)  NOT NULL,
    confidence      DOUBLE PRECISION NOT NULL,
    reasoning       TEXT,
    bar_time        TIMESTAMPTZ,
    bar_open        DOUBLE PRECISION,
    bar_high        DOUBLE PRECISION,
    bar_low         DOUBLE PRECISION,
    bar_close       DOUBLE PRECISION,
    indicators      JSONB NOT NULL DEFAULT '{}',
    executed        BOOLEAN NOT NULL DEFAULT FALSE,
    trade_id        BIGINT,
    CONSTRAINT uq_signal_id UNIQUE (signal_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_signals_dedup ON ml_signals (bot_name, symbol, bar_time) WHERE bar_time IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_signals_bot_sym ON ml_signals (bot_name, symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_vote ON ml_signals (vote) WHERE vote IN ('BUY', 'SELL');

-- Demo trades
CREATE TABLE IF NOT EXISTS ml_trades (
    id              BIGSERIAL PRIMARY KEY,
    trade_id        UUID DEFAULT gen_random_uuid(),
    signal_id       UUID NOT NULL,
    bot_name        VARCHAR(20) NOT NULL,
    model_name      VARCHAR(40) NOT NULL,
    symbol          VARCHAR(20) NOT NULL,
    timeframe       VARCHAR(4)  NOT NULL,
    account_id      BIGINT NOT NULL,
    side            VARCHAR(4)  NOT NULL,
    entry_price     DOUBLE PRECISION,
    sl_price        DOUBLE PRECISION,
    tp_price        DOUBLE PRECISION,
    volume_lots     DOUBLE PRECISION NOT NULL,
    wire_volume     BIGINT,
    ticket          VARCHAR(40),
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    exit_price      DOUBLE PRECISION,
    exit_reason     VARCHAR(30),
    closed_at       TIMESTAMPTZ,
    pnl             DOUBLE PRECISION,
    outcome         VARCHAR(10),
    duration_minutes DOUBLE PRECISION,
    account_balance  DOUBLE PRECISION,
    account_equity   DOUBLE PRECISION,
    signal_confidence DOUBLE PRECISION,
    signal_indicators JSONB DEFAULT '{}',
    CONSTRAINT uq_trade_id UNIQUE (trade_id)
);
CREATE INDEX IF NOT EXISTS idx_trades_open ON ml_trades (closed_at) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_trades_ticket ON ml_trades (account_id, ticket) WHERE ticket IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_trades_bot_sym ON ml_trades (bot_name, symbol, opened_at DESC);

-- Runtime state (replaces open_trades.json)
CREATE TABLE IF NOT EXISTS ml_collector_state (
    key        VARCHAR(100) PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Grant all privileges to the collector role
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO glitchml;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO glitchml;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO glitchml;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO glitchml;
