-- 0003_oracle_risk_gate.sql
-- Oracle PRE-TRADE risk gate. Sits in front of every bot's place_market_order
-- call and blocks trades that would violate portfolio-level risk limits.
--
-- Two tables:
--   ml_oracle_risk_limits  configurable limits per scope (symbol / bucket / global)
--   ml_oracle_blocks       every blocked trade attempt, with full reason detail

CREATE TABLE IF NOT EXISTS ml_oracle_risk_limits (
    -- 'symbol'  → cap total open lots on one instrument across all bots
    -- 'bucket'  → cap total open lots in a correlation bucket (USD_MAJOR, JPY_CROSS, ...)
    -- 'global'  → cap total open lots across the entire portfolio
    -- 'symbol_directional' → cap net directional exposure on one symbol (BUY-SELL)
    -- 'bucket_directional' → cap net directional exposure in a bucket
    scope_type   VARCHAR(24) NOT NULL,
    scope_key    VARCHAR(40) NOT NULL,   -- 'EURUSD' | 'USD_MAJOR' | 'ALL'
    max_lots     DOUBLE PRECISION NOT NULL,
    max_trades   INTEGER,                -- optional open-trade count cap
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    notes        TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scope_type, scope_key)
);

CREATE TABLE IF NOT EXISTS ml_oracle_blocks (
    id            BIGSERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    bot_name      VARCHAR(20) NOT NULL,
    symbol        VARCHAR(20) NOT NULL,
    side          VARCHAR(4)  NOT NULL,
    proposed_lots DOUBLE PRECISION NOT NULL,
    block_reason  VARCHAR(40) NOT NULL,
    -- {"scope_type":"symbol","scope_key":"EURUSD","limit":2.0,"current":1.8,"would_be":2.3}
    block_detail  JSONB NOT NULL DEFAULT '{}',
    signal_id     UUID
);

CREATE INDEX IF NOT EXISTS idx_blocks_bot_sym_time ON ml_oracle_blocks (bot_name, symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_blocks_reason       ON ml_oracle_blocks (block_reason, created_at DESC);

-- Seed defaults. These are intentionally conservative; tune via UPDATE.
-- Per-symbol cap: no more than 3 lots total open across all bots for any one instrument.
-- Per-bucket cap: tighter on USD majors (correlated), looser on metals/equity.
-- Global cap: hard ceiling across the entire portfolio.
INSERT INTO ml_oracle_risk_limits (scope_type, scope_key, max_lots, max_trades, notes) VALUES
    ('global',    'ALL',         10.0, 30,  'portfolio-wide ceiling across all bots + symbols'),

    ('bucket',    'USD_MAJOR',    5.0, 12,  'EUR/GBP/AUD/NZD/JPY/CHF/CAD vs USD — highly correlated'),
    ('bucket',    'JPY_CROSS',    2.0,  4,  'GBPJPY / EURJPY'),
    ('bucket',    'METALS',       2.0,  4,  'XAUUSD / XAGUSD'),
    ('bucket',    'ENERGY',       1.5,  2,  'XTIUSD'),
    ('bucket',    'EQUITY_US',    3.0,  4,  'US500 / US100'),
    ('bucket',    'EQUITY_EU',    3.0,  4,  'GER40 / UK100'),
    ('bucket',    'EQUITY_AS',    2.0,  2,  'JPN225'),

    ('symbol',    'EURUSD',       2.0,  3,  NULL),
    ('symbol',    'GBPUSD',       2.0,  3,  NULL),
    ('symbol',    'USDJPY',       2.0,  3,  NULL),
    ('symbol',    'USDCHF',       1.5,  2,  NULL),
    ('symbol',    'AUDUSD',       1.5,  2,  NULL),
    ('symbol',    'NZDUSD',       1.5,  2,  NULL),
    ('symbol',    'USDCAD',       1.5,  2,  NULL),
    ('symbol',    'GBPJPY',       1.0,  2,  NULL),
    ('symbol',    'EURJPY',       1.0,  2,  NULL),
    ('symbol',    'XAUUSD',       1.5,  3,  NULL),
    ('symbol',    'XAGUSD',       1.0,  2,  NULL),
    ('symbol',    'XTIUSD',       1.0,  2,  NULL),
    ('symbol',    'US500',        2.0,  3,  NULL),
    ('symbol',    'US100',        2.0,  3,  NULL),
    ('symbol',    'GER40',        2.0,  3,  NULL),
    ('symbol',    'UK100',        1.5,  2,  NULL),
    ('symbol',    'JPN225',       1.5,  2,  NULL)
ON CONFLICT (scope_type, scope_key) DO NOTHING;
