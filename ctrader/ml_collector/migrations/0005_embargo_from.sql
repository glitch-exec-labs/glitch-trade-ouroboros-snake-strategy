-- 0005_embargo_from.sql
-- Adds an optional start-time to the embargo window. Currently embargoes
-- are active from the moment the row is inserted until embargo_until.
-- With embargo_from, scheduled macro events (CPI, FOMC, NFP) inserted
-- HOURS in advance by the economic-calendar poller don't block trading
-- that whole time — they only become active in a tight window around
-- the event itself (e.g., 30 min before through 60 min after).
--
-- NULL embargo_from = "active since creation" (backward compatible for
-- reactive news classifications where we only know AFTER the fact).

ALTER TABLE ml_news_events
    ADD COLUMN IF NOT EXISTS embargo_from TIMESTAMPTZ;

-- Narrow new index: live embargoes (either NULL-from-meaning-active or from ≤ now)
CREATE INDEX IF NOT EXISTS idx_news_embargo_window
    ON ml_news_events (embargo_until)
    WHERE embargo_until IS NOT NULL;
