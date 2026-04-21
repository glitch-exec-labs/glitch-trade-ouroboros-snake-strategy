-- 0004_news_guard.sql
-- News-event embargo layer: the news_guard task polls newsdata.io, classifies
-- each article against ml_news_rules, and when a high-impact event is detected
-- it writes a row to ml_news_events with embargo_until = published_at +
-- embargo_minutes. Oracle.check_trade_allowed consults this table and blocks
-- trades on affected symbols/buckets during the embargo window.

CREATE TABLE IF NOT EXISTS ml_news_rules (
    id               SERIAL PRIMARY KEY,
    rule_name        VARCHAR(60) NOT NULL UNIQUE,
    -- PostgreSQL ILIKE pattern OR (if starts with '~') a regex. Matched against
    -- title + ' ' + description.
    pattern          TEXT NOT NULL,
    event_type       VARCHAR(40) NOT NULL,   -- 'cpi' | 'fomc' | 'ecb' | 'bojo' | 'geopolitical' | ...
    impact           VARCHAR(10) NOT NULL,   -- 'high' | 'medium' | 'low'
    embargo_minutes  INTEGER NOT NULL DEFAULT 30,
    affected_buckets TEXT[] NOT NULL DEFAULT '{}',  -- ['USD_MAJOR','METALS',...]
    affected_symbols TEXT[] NOT NULL DEFAULT '{}',  -- optional symbol-specific override
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    notes            TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ml_news_events (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    article_id      VARCHAR(80) NOT NULL UNIQUE,  -- newsdata.io article_id for dedup
    title           TEXT NOT NULL,
    description     TEXT,
    link            TEXT,
    source          VARCHAR(120),
    published_at    TIMESTAMPTZ,
    category        TEXT[],
    country         TEXT[],
    -- Classification (populated when a rule matches; NULL for passthrough articles)
    matched_rule_id INTEGER REFERENCES ml_news_rules(id),
    event_type      VARCHAR(40),
    impact          VARCHAR(10),
    affected_buckets TEXT[] NOT NULL DEFAULT '{}',
    affected_symbols TEXT[] NOT NULL DEFAULT '{}',
    embargo_until   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_news_embargo ON ml_news_events (embargo_until)
    WHERE embargo_until IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_news_impact_time ON ml_news_events (impact, published_at DESC)
    WHERE impact IN ('high', 'medium');

-- Seed rules. ILIKE patterns (not regex) — case-insensitive substring match.
-- Tune via UPDATE. affected_buckets mirror oracle.CORRELATION_BUCKETS.
INSERT INTO ml_news_rules (rule_name, pattern, event_type, impact, embargo_minutes, affected_buckets) VALUES
    -- US macro
    ('us_cpi',          '%CPI%',                         'cpi',          'high',   60, ARRAY['USD_MAJOR','METALS','EQUITY_US','JPY_CROSS']),
    ('us_inflation',    '%inflation%',                   'inflation',    'medium', 45, ARRAY['USD_MAJOR','METALS','EQUITY_US']),
    ('us_fomc',         '%FOMC%',                        'fomc',         'high',   90, ARRAY['USD_MAJOR','METALS','EQUITY_US','JPY_CROSS']),
    ('us_fed_rate',     '%Fed%rate%',                    'fed_rate',     'high',   90, ARRAY['USD_MAJOR','METALS','EQUITY_US','JPY_CROSS']),
    ('us_powell',       '%Powell%',                      'fed_speech',   'medium', 30, ARRAY['USD_MAJOR','EQUITY_US']),
    ('us_nfp',          '%nonfarm payroll%',             'nfp',          'high',   60, ARRAY['USD_MAJOR','METALS','EQUITY_US']),
    ('us_nfp_short',    '%NFP%',                         'nfp',          'high',   60, ARRAY['USD_MAJOR','METALS','EQUITY_US']),
    ('us_unemployment', '%unemployment rate%',           'unemployment', 'medium', 45, ARRAY['USD_MAJOR','EQUITY_US']),
    ('us_gdp',          '%GDP%',                         'gdp',          'medium', 45, ARRAY['USD_MAJOR','EQUITY_US']),
    ('us_pmi',          '%PMI%',                         'pmi',          'medium', 30, ARRAY['USD_MAJOR','EQUITY_US']),

    -- Central banks
    ('ecb',             '%ECB%',                         'ecb',          'high',   60, ARRAY['USD_MAJOR','JPY_CROSS','EQUITY_EU']),
    ('ecb_lagarde',     '%Lagarde%',                     'ecb_speech',   'medium', 30, ARRAY['USD_MAJOR','EQUITY_EU']),
    ('boe',             '%Bank of England%',             'boe',          'high',   60, ARRAY['USD_MAJOR','JPY_CROSS','EQUITY_EU']),
    ('boj',             '%Bank of Japan%',               'boj',          'high',   60, ARRAY['USD_MAJOR','JPY_CROSS','EQUITY_AS']),
    ('boj_ueda',        '%Ueda%',                        'boj_speech',   'medium', 30, ARRAY['JPY_CROSS','EQUITY_AS']),
    ('rba',             '%Reserve Bank of Australia%',   'rba',          'medium', 45, ARRAY['USD_MAJOR']),
    ('rate_decision',   '%rate decision%',               'rate_decision','high',   60, ARRAY['USD_MAJOR','JPY_CROSS']),
    ('interest_rate',   '%interest rate%',               'interest_rate','medium', 45, ARRAY['USD_MAJOR','JPY_CROSS']),

    -- Geopolitical / risk-off
    ('war',             '%war%',                         'geopolitical', 'high',  120, ARRAY['METALS','ENERGY','EQUITY_US','EQUITY_EU','EQUITY_AS']),
    ('invasion',        '%invasion%',                    'geopolitical', 'high',  120, ARRAY['METALS','ENERGY','EQUITY_US','EQUITY_EU']),
    ('sanctions',       '%sanctions%',                   'geopolitical', 'medium', 60, ARRAY['METALS','ENERGY','EQUITY_EU']),
    ('strike_military', '%military strike%',             'geopolitical', 'high',  120, ARRAY['METALS','ENERGY','EQUITY_US','EQUITY_EU']),
    ('oil_supply',      '%oil supply%',                  'oil_supply',   'medium', 45, ARRAY['ENERGY']),
    ('opec',            '%OPEC%',                        'opec',         'high',   60, ARRAY['ENERGY']),

    -- Equity-specific
    ('earnings',        '%earnings%',                    'earnings',     'low',    15, ARRAY['EQUITY_US']),
    ('market_crash',    '%market crash%',                'market_crash', 'high',  180, ARRAY['EQUITY_US','EQUITY_EU','EQUITY_AS','METALS'])
ON CONFLICT (rule_name) DO NOTHING;
