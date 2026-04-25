-- 0007_crypto_risk_limits.sql
-- Add risk caps for the 3 crypto symbols + new CRYPTO correlation bucket.
-- Conservative initial sizing pending observed model behaviour:
--   - Crypto volatility is 3-5x forex; a 5% intraday move on BTCUSD is routine.
--   - Caps measured in BROKER LOTS (not standard forex lots). cTrader's
--     lotSize for BTCUSD/ETHUSD/SOLUSD = 100 wire units = 1 unit of the
--     underlying. So 8 lots of BTC ~ 8 BTC ~ $480k notional at $60k BTC.
--     That's a portfolio-relevant ceiling, not a per-trade target.
--   - Tune after a week of live observation via UPDATE — no code changes needed.
-- NOTE: crypto is intentionally NOT added to news_guard.COUNTRY_BUCKETS.
-- Macro events (CPI, FOMC) DO move crypto, but the relationship is noisier
-- and lagged. Letting crypto trade through them gives us per-event
-- baseline data; revisit once we have ~2 weeks of crypto signals.

INSERT INTO ml_oracle_risk_limits (scope_type, scope_key, max_lots, max_trades, notes) VALUES
    ('bucket',    'CRYPTO',    16,  28,  '24/7 trading; 3 USD-quoted majors'),
    ('symbol',    'BTCUSD',     6,  12,  'lotSize=100; 1 lot ~= 1 BTC'),
    ('symbol',    'ETHUSD',     8,  16,  'lotSize=100; 1 lot ~= 1 ETH'),
    ('symbol',    'SOLUSD',    12,  24,  'lotSize=100; minVol=100 (lower granularity)')
ON CONFLICT (scope_type, scope_key) DO UPDATE
    SET max_lots   = EXCLUDED.max_lots,
        max_trades = EXCLUDED.max_trades,
        notes      = EXCLUDED.notes,
        updated_at = NOW();

-- Also bump the global cap modestly: adding 3 24/7 instruments will increase
-- portfolio-wide concurrent exposure during weekends.
UPDATE ml_oracle_risk_limits SET max_lots = 80, max_trades = 160
    WHERE scope_type = 'global' AND scope_key = 'ALL';
