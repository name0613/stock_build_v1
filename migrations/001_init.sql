-- PostgreSQL 16 reference migration. Runtime also calls SQLAlchemy create_all for first boot.
CREATE TABLE IF NOT EXISTS stocks (
  stock_id VARCHAR(16) PRIMARY KEY, stock_name VARCHAR(128) NOT NULL, market VARCHAR(16) NOT NULL,
  industry VARCHAR(128), security_type VARCHAR(64), is_common_stock BOOLEAN NOT NULL DEFAULT TRUE,
  source_date DATE, fetched_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_stocks_market ON stocks(market);
CREATE TABLE IF NOT EXISTS institutional_daily (
  id BIGSERIAL PRIMARY KEY, stock_id VARCHAR(16) NOT NULL REFERENCES stocks(stock_id), source_date DATE NOT NULL,
  foreign_net DOUBLE PRECISION, foreign_dealer_self_net DOUBLE PRECISION, investment_trust_net DOUBLE PRECISION,
  dealer_net DOUBLE PRECISION, dealer_aggregate_net DOUBLE PRECISION, dealer_self_net DOUBLE PRECISION, dealer_hedging_net DOUBLE PRECISION,
  institutional_net DOUBLE PRECISION, source_dataset VARCHAR(100) NOT NULL, fetched_at TIMESTAMPTZ NOT NULL,
  UNIQUE(stock_id, source_date)
);
CREATE INDEX IF NOT EXISTS ix_institutional_daily_stock_date ON institutional_daily(stock_id, source_date);
CREATE TABLE IF NOT EXISTS foreign_shareholding_daily (
  id BIGSERIAL PRIMARY KEY, stock_id VARCHAR(16) NOT NULL REFERENCES stocks(stock_id), source_date DATE NOT NULL,
  foreign_investment_shares DOUBLE PRECISION, foreign_investment_shares_ratio DOUBLE PRECISION,
  number_of_shares_issued DOUBLE PRECISION, recently_declare_date VARCHAR(32), source_dataset VARCHAR(100) NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL, UNIQUE(stock_id, source_date)
);
CREATE TABLE IF NOT EXISTS holding_distribution (
  id BIGSERIAL PRIMARY KEY, stock_id VARCHAR(16) NOT NULL REFERENCES stocks(stock_id), source_date DATE NOT NULL,
  holding_shares_level VARCHAR(128) NOT NULL, holding_shares_threshold INTEGER, people DOUBLE PRECISION,
  percent DOUBLE PRECISION, shares DOUBLE PRECISION, unit VARCHAR(32), source_dataset VARCHAR(100) NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL, UNIQUE(stock_id, source_date, holding_shares_level)
);
CREATE TABLE IF NOT EXISTS broker_daily (
  id BIGSERIAL PRIMARY KEY, stock_id VARCHAR(16) NOT NULL REFERENCES stocks(stock_id), source_date DATE NOT NULL,
  securities_trader_id VARCHAR(32) NOT NULL, securities_trader_name VARCHAR(128), buy_volume DOUBLE PRECISION,
  sell_volume DOUBLE PRECISION, net_volume DOUBLE PRECISION, buy_amount DOUBLE PRECISION, sell_amount DOUBLE PRECISION,
  avg_buy_price DOUBLE PRECISION, avg_sell_price DOUBLE PRECISION, source_dataset VARCHAR(100) NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL, UNIQUE(stock_id, source_date, securities_trader_id)
);
CREATE TABLE IF NOT EXISTS price_daily (
  id BIGSERIAL PRIMARY KEY, stock_id VARCHAR(16) NOT NULL REFERENCES stocks(stock_id), source_date DATE NOT NULL,
  close DOUBLE PRECISION, volume DOUBLE PRECISION, change DOUBLE PRECISION, source_dataset VARCHAR(100) NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL, UNIQUE(stock_id, source_date)
);
CREATE TABLE IF NOT EXISTS accumulation_features (
  id BIGSERIAL PRIMARY KEY, stock_id VARCHAR(16) NOT NULL REFERENCES stocks(stock_id), source_date DATE NOT NULL,
  values JSONB NOT NULL DEFAULT '{}'::jsonb, coverage JSONB NOT NULL DEFAULT '{}'::jsonb, latest_source_date VARCHAR(32),
  calculated_at TIMESTAMPTZ NOT NULL, UNIQUE(stock_id, source_date)
);
CREATE TABLE IF NOT EXISTS accumulation_scores (
  id BIGSERIAL PRIMARY KEY, stock_id VARCHAR(16) NOT NULL REFERENCES stocks(stock_id), source_date DATE NOT NULL,
  score DOUBLE PRECISION, status VARCHAR(32) NOT NULL, score_version VARCHAR(64) NOT NULL, components JSONB NOT NULL DEFAULT '{}'::jsonb,
  explanation JSONB NOT NULL DEFAULT '[]'::jsonb, coverage JSONB NOT NULL DEFAULT '{}'::jsonb, calculated_at TIMESTAMPTZ NOT NULL,
  UNIQUE(stock_id, source_date, score_version)
);
CREATE INDEX IF NOT EXISTS ix_scores_score_status ON accumulation_scores(score, status);
CREATE TABLE IF NOT EXISTS data_sync_status (
  dataset VARCHAR(100) PRIMARY KEY, status VARCHAR(32) NOT NULL, latest_source_date DATE, last_successful_sync TIMESTAMPTZ,
  last_error_code VARCHAR(64), last_error TEXT, records INTEGER NOT NULL DEFAULT 0, metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS job_runs (
  id BIGSERIAL PRIMARY KEY, dataset VARCHAR(100) NOT NULL, requested_date DATE, status VARCHAR(32) NOT NULL,
  started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ, records INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER,
  error TEXT, retry_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS score_versions (
  version VARCHAR(64) PRIMARY KEY, config JSONB NOT NULL, explanation TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS major_shareholder_disclosures (
  id BIGSERIAL PRIMARY KEY, stock_id VARCHAR(16) NOT NULL REFERENCES stocks(stock_id), holder VARCHAR(255) NOT NULL,
  declare_date DATE NOT NULL, holding_shares DOUBLE PRECISION, holding_ratio DOUBLE PRECISION, change DOUBLE PRECISION,
  source VARCHAR(255) NOT NULL, UNIQUE(stock_id, holder, declare_date)
);
