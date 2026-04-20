-- 0002_oracle.sql
-- Oracle ensemble layer: records weighted-vote decisions across all 6 bots.

CREATE TABLE IF NOT EXISTS ml_oracle_weights (
    bot_name         VARCHAR(20) PRIMARY KEY,
    weight           DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    can_veto         BOOLEAN NOT NULL DEFAULT FALSE,
    freshness_sec    INTEGER NOT NULL DEFAULT 300,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS ml_oracle_decisions (
    id                   BIGSERIAL PRIMARY KEY,
    decision_id          UUID DEFAULT gen_random_uuid(),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol               VARCHAR(20) NOT NULL,
    decision             VARCHAR(8)  NOT NULL,
    decision_confidence  DOUBLE PRECISION NOT NULL,
    buy_score            DOUBLE PRECISION NOT NULL,
    sell_score           DOUBLE PRECISION NOT NULL,
    hold_score           DOUBLE PRECISION NOT NULL,
    contributors         JSONB NOT NULL DEFAULT '{}',
    abstain_reason       VARCHAR(40),
    mode                 VARCHAR(10) NOT NULL DEFAULT 'shadow',
    trade_id             UUID,
    CONSTRAINT uq_oracle_decision_id UNIQUE (decision_id)
);

CREATE INDEX IF NOT EXISTS idx_oracle_sym_time  ON ml_oracle_decisions (symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_oracle_decision  ON ml_oracle_decisions (decision) WHERE decision IN ('BUY','SELL');
CREATE INDEX IF NOT EXISTS idx_oracle_mode_time ON ml_oracle_decisions (mode, created_at DESC);

INSERT INTO ml_oracle_weights (bot_name, weight, can_veto, freshness_sec, notes) VALUES
    ('hydra',    0.5, TRUE,   180,  'M1 tactical; high cadence; veto on regime collapse'),
    ('viper',    0.8, FALSE,  600,  'M5 momentum'),
    ('mamba',    1.0, FALSE,  1800, 'M15 mean reversion'),
    ('taipan',   1.2, FALSE,  2400, 'M30 session breakout'),
    ('cobra',    1.5, FALSE,  4800, 'H1 trend structure'),
    ('anaconda', 1.8, FALSE,  18000,'H4 volume profile; slowest but most deliberative')
ON CONFLICT (bot_name) DO NOTHING;
