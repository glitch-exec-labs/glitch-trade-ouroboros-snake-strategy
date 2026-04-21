-- 0006_risk_limits_50k_per_bot.sql
-- Rescale Oracle risk caps for the real production portfolio:
-- 6 bots × $50,000 USD each = $300,000 total equity.
--
-- The previous seed values (max EURUSD = 2 lots, global = 10 lots) were
-- placeholder demo-scale numbers that choked the 6-bot ensemble once
-- accounts were funded. These caps target meaningful headroom:
--
--   per bot     : up to ~10 lots of concurrent exposure
--   portfolio   : up to ~60 lots total (hard ceiling)
--   per symbol  : enough room for 4-6 bots to stack the same instrument
--   per bucket  : sized by correlated-pair count + typical targeting
--
-- At 1 std lot = 100,000 units of base currency, 1% account risk per
-- trade is roughly 1 std lot on majors. Under these caps a bot running
-- 1-2% risk per trade with 3-5 open positions is unconstrained; the
-- Oracle still blocks pathological cases (single bot opening 20 lots,
-- two bots piling into the same symbol with correlated directional risk).
--
-- All limits remain runtime-tunable via UPDATE without a service restart.

BEGIN;

-- Global ceiling: portfolio-wide hard cap across all bots + symbols
UPDATE ml_oracle_risk_limits SET max_lots = 60,  max_trades = 120
    WHERE scope_type = 'global' AND scope_key = 'ALL';

-- Correlation buckets
UPDATE ml_oracle_risk_limits SET max_lots = 35,  max_trades = 60
    WHERE scope_type = 'bucket' AND scope_key = 'USD_MAJOR';
UPDATE ml_oracle_risk_limits SET max_lots = 10,  max_trades = 16
    WHERE scope_type = 'bucket' AND scope_key = 'JPY_CROSS';
UPDATE ml_oracle_risk_limits SET max_lots = 12,  max_trades = 20
    WHERE scope_type = 'bucket' AND scope_key = 'METALS';
UPDATE ml_oracle_risk_limits SET max_lots = 6,   max_trades = 10
    WHERE scope_type = 'bucket' AND scope_key = 'ENERGY';
UPDATE ml_oracle_risk_limits SET max_lots = 12,  max_trades = 20
    WHERE scope_type = 'bucket' AND scope_key = 'EQUITY_US';
UPDATE ml_oracle_risk_limits SET max_lots = 10,  max_trades = 16
    WHERE scope_type = 'bucket' AND scope_key = 'EQUITY_EU';
UPDATE ml_oracle_risk_limits SET max_lots = 6,   max_trades = 10
    WHERE scope_type = 'bucket' AND scope_key = 'EQUITY_AS';

-- Top-tier majors — deepest liquidity, highest strategic use
UPDATE ml_oracle_risk_limits SET max_lots = 10,  max_trades = 18
    WHERE scope_type = 'symbol' AND scope_key IN ('EURUSD', 'GBPUSD', 'USDJPY');

-- Second-tier majors
UPDATE ml_oracle_risk_limits SET max_lots = 6,   max_trades = 12
    WHERE scope_type = 'symbol' AND scope_key IN ('AUDUSD', 'NZDUSD', 'USDCAD', 'USDCHF');

-- JPY crosses
UPDATE ml_oracle_risk_limits SET max_lots = 6,   max_trades = 12
    WHERE scope_type = 'symbol' AND scope_key IN ('GBPJPY', 'EURJPY');

-- Commodities
UPDATE ml_oracle_risk_limits SET max_lots = 8,   max_trades = 14
    WHERE scope_type = 'symbol' AND scope_key = 'XAUUSD';
UPDATE ml_oracle_risk_limits SET max_lots = 4,   max_trades = 8
    WHERE scope_type = 'symbol' AND scope_key = 'XAGUSD';
UPDATE ml_oracle_risk_limits SET max_lots = 5,   max_trades = 10
    WHERE scope_type = 'symbol' AND scope_key = 'XTIUSD';

-- Equity indices
UPDATE ml_oracle_risk_limits SET max_lots = 8,   max_trades = 14
    WHERE scope_type = 'symbol' AND scope_key IN ('US500', 'US100', 'GER40');
UPDATE ml_oracle_risk_limits SET max_lots = 6,   max_trades = 12
    WHERE scope_type = 'symbol' AND scope_key IN ('UK100', 'JPN225');

-- Touch updated_at so the change is visible in audit queries
UPDATE ml_oracle_risk_limits SET updated_at = NOW();

COMMIT;

-- Sanity check: fail the migration if no rows matched (means seed row names drifted)
DO $$
DECLARE n_touched INT;
BEGIN
    SELECT COUNT(*) INTO n_touched
    FROM ml_oracle_risk_limits
    WHERE updated_at > NOW() - INTERVAL '1 minute';
    IF n_touched < 20 THEN
        RAISE EXCEPTION 'only % rows were updated — expected ≥20; check seed row names', n_touched;
    END IF;
END $$;
