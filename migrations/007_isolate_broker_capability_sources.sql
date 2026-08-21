CREATE TABLE IF NOT EXISTS broker_daily_source_quarantine (
  original_id BIGINT PRIMARY KEY,
  stock_id VARCHAR(16),
  source_date DATE,
  securities_trader_id VARCHAR(32),
  securities_trader_name VARCHAR(128),
  buy_volume DOUBLE PRECISION,
  sell_volume DOUBLE PRECISION,
  net_volume DOUBLE PRECISION,
  buy_amount DOUBLE PRECISION,
  sell_amount DOUBLE PRECISION,
  avg_buy_price DOUBLE PRECISION,
  avg_sell_price DOUBLE PRECISION,
  source_dataset VARCHAR(100),
  provider_report_complete BOOLEAN,
  fetched_at TIMESTAMPTZ,
  quarantine_reason VARCHAR(128) NOT NULL,
  quarantined_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS broker_source_revision_quarantine (
  original_id BIGINT PRIMARY KEY,
  dataset VARCHAR(100) NOT NULL,
  stock_id VARCHAR(16),
  source_date DATE,
  natural_key VARCHAR(255) NOT NULL,
  payload JSONB NOT NULL,
  content_hash VARCHAR(64) NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL,
  quarantine_reason VARCHAR(128) NOT NULL,
  quarantined_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS broker_source_affected_stocks (
  stock_id VARCHAR(16) PRIMARY KEY,
  detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  remediation_state VARCHAR(64) NOT NULL DEFAULT 'QUARANTINED_REQUIRES_AUTHORITATIVE_REBUILD',
  remediated_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS broker_source_isolation_audit (
  migration_version VARCHAR(64) PRIMARY KEY,
  prohibited_broker_rows INTEGER NOT NULL,
  prohibited_source_revisions INTEGER NOT NULL,
  affected_stocks INTEGER NOT NULL,
  invalidated_features INTEGER NOT NULL,
  invalidated_v4_scores INTEGER NOT NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO broker_source_affected_stocks(stock_id)
SELECT DISTINCT stock_id
FROM broker_daily
WHERE source_dataset IS DISTINCT FROM 'TaiwanStockTradingDailyReport'
ON CONFLICT (stock_id) DO NOTHING;

INSERT INTO broker_source_affected_stocks(stock_id)
SELECT DISTINCT stock_id
FROM source_revisions
WHERE dataset = 'TaiwanStockTradingDailyReportSecIdAgg' AND stock_id IS NOT NULL
ON CONFLICT (stock_id) DO NOTHING;

INSERT INTO broker_daily_source_quarantine(
  original_id, stock_id, source_date, securities_trader_id, securities_trader_name,
  buy_volume, sell_volume, net_volume, buy_amount, sell_amount, avg_buy_price,
  avg_sell_price, source_dataset, provider_report_complete, fetched_at, quarantine_reason
)
SELECT
  id, stock_id, source_date, securities_trader_id, securities_trader_name,
  buy_volume, sell_volume, net_volume, buy_amount, sell_amount, avg_buy_price,
  avg_sell_price, source_dataset, provider_report_complete, fetched_at,
  'NON_PRODUCTION_BROKER_SOURCE'
FROM broker_daily
WHERE source_dataset IS DISTINCT FROM 'TaiwanStockTradingDailyReport'
ON CONFLICT (original_id) DO NOTHING;

INSERT INTO broker_source_revision_quarantine(
  original_id, dataset, stock_id, source_date, natural_key, payload, content_hash,
  fetched_at, quarantine_reason
)
SELECT
  id, dataset, stock_id, source_date, natural_key, payload, content_hash,
  fetched_at, 'CAPABILITY_ONLY_SOURCE_REVISION'
FROM source_revisions
WHERE dataset = 'TaiwanStockTradingDailyReportSecIdAgg'
ON CONFLICT (original_id) DO NOTHING;

INSERT INTO broker_source_isolation_audit(
  migration_version, prohibited_broker_rows, prohibited_source_revisions,
  affected_stocks, invalidated_features, invalidated_v4_scores
)
SELECT
  '007_isolate_broker_capability_sources',
  (SELECT count(*) FROM broker_daily_source_quarantine),
  (SELECT count(*) FROM broker_source_revision_quarantine),
  (SELECT count(*) FROM broker_source_affected_stocks),
  (SELECT count(*) FROM accumulation_features WHERE stock_id IN (SELECT stock_id FROM broker_source_affected_stocks)),
  (SELECT count(*) FROM accumulation_scores WHERE score_version = 's-only-v4' AND stock_id IN (SELECT stock_id FROM broker_source_affected_stocks))
ON CONFLICT (migration_version) DO NOTHING;

DELETE FROM accumulation_features
WHERE stock_id IN (SELECT stock_id FROM broker_source_affected_stocks);

DELETE FROM accumulation_scores
WHERE score_version = 's-only-v4'
  AND stock_id IN (SELECT stock_id FROM broker_source_affected_stocks);

DELETE FROM source_revisions
WHERE dataset = 'TaiwanStockTradingDailyReportSecIdAgg';

DELETE FROM broker_daily
WHERE source_dataset IS DISTINCT FROM 'TaiwanStockTradingDailyReport';

ALTER TABLE broker_daily
  DROP CONSTRAINT IF EXISTS ck_broker_daily_official_source;

ALTER TABLE broker_daily
  ADD CONSTRAINT ck_broker_daily_official_source
  CHECK (source_dataset = 'TaiwanStockTradingDailyReport');
