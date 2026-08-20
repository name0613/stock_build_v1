-- Seed legacy normalized rows before any post-upgrade correction can replace
-- their only copy.  The migration is idempotent and preserves the original
-- fetched_at when it exists; rows without provenance are explicitly marked
-- with a legacy fetched timestamp and are not presented as pre-cutoff proof.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

INSERT INTO source_revisions(dataset, stock_id, source_date, natural_key, payload, content_hash, fetched_at)
SELECT 'TaiwanStockInstitutionalInvestorsBuySellWide', t.stock_id, t.source_date,
  replace(replace(jsonb_build_object('source_date', t.source_date, 'stock_id', t.stock_id)::text, ': ', ':'), ', ', ','),
  to_jsonb(t) - 'id', encode(digest((to_jsonb(t) - 'id')::text, 'sha256'), 'hex'), coalesce(t.fetched_at, now())
FROM institutional_daily t ON CONFLICT (dataset, natural_key, content_hash) DO NOTHING;

INSERT INTO source_revisions(dataset, stock_id, source_date, natural_key, payload, content_hash, fetched_at)
SELECT 'TaiwanStockShareholding', t.stock_id, t.source_date,
  replace(replace(jsonb_build_object('source_date', t.source_date, 'stock_id', t.stock_id)::text, ': ', ':'), ', ', ','),
  to_jsonb(t) - 'id', encode(digest((to_jsonb(t) - 'id')::text, 'sha256'), 'hex'), coalesce(t.fetched_at, now())
FROM foreign_shareholding_daily t ON CONFLICT (dataset, natural_key, content_hash) DO NOTHING;

INSERT INTO source_revisions(dataset, stock_id, source_date, natural_key, payload, content_hash, fetched_at)
SELECT 'TaiwanStockHoldingSharesPer', t.stock_id, t.source_date,
  replace(replace(jsonb_build_object('holding_shares_level', t.holding_shares_level, 'source_date', t.source_date, 'stock_id', t.stock_id)::text, ': ', ':'), ', ', ','),
  to_jsonb(t) - 'id', encode(digest((to_jsonb(t) - 'id')::text, 'sha256'), 'hex'), coalesce(t.fetched_at, now())
FROM holding_distribution t ON CONFLICT (dataset, natural_key, content_hash) DO NOTHING;

INSERT INTO source_revisions(dataset, stock_id, source_date, natural_key, payload, content_hash, fetched_at)
SELECT 'TaiwanStockTradingDailyReport', t.stock_id, t.source_date,
  replace(replace(jsonb_build_object('securities_trader_id', t.securities_trader_id, 'source_date', t.source_date, 'stock_id', t.stock_id)::text, ': ', ':'), ', ', ','),
  to_jsonb(t) - 'id', encode(digest((to_jsonb(t) - 'id')::text, 'sha256'), 'hex'), coalesce(t.fetched_at, now())
FROM broker_daily t ON CONFLICT (dataset, natural_key, content_hash) DO NOTHING;

INSERT INTO source_revisions(dataset, stock_id, source_date, natural_key, payload, content_hash, fetched_at)
SELECT 'TaiwanStockPrice', t.stock_id, t.source_date,
  replace(replace(jsonb_build_object('source_date', t.source_date, 'stock_id', t.stock_id)::text, ': ', ':'), ', ', ','),
  to_jsonb(t) - 'id', encode(digest((to_jsonb(t) - 'id')::text, 'sha256'), 'hex'), coalesce(t.fetched_at, now())
FROM price_daily t ON CONFLICT (dataset, natural_key, content_hash) DO NOTHING;
