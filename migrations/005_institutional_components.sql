-- Preserve the provider's aggregate dealer field separately from the
-- non-overlapping component used by the score.
ALTER TABLE institutional_daily ADD COLUMN IF NOT EXISTS dealer_aggregate_net DOUBLE PRECISION;
