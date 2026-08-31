-- Capital-aware v7 is additive.  Existing s-only-v6 rows and formula remain
-- immutable; this table stores a second, independently versioned snapshot.
ALTER TABLE price_daily ADD COLUMN IF NOT EXISTS trading_money DOUBLE PRECISION;
ALTER TABLE price_daily ADD COLUMN IF NOT EXISTS trading_turnover DOUBLE PRECISION;

CREATE TABLE IF NOT EXISTS capital_aware_scores (
  id BIGSERIAL PRIMARY KEY,
  stock_id VARCHAR(16) NOT NULL REFERENCES stocks(stock_id),
  source_date DATE NOT NULL,
  score DOUBLE PRECISION,
  status VARCHAR(64) NOT NULL,
  score_version VARCHAR(64) NOT NULL,
  large_capital_score DOUBLE PRECISION,
  high_confidence_score DOUBLE PRECISION,
  components JSONB NOT NULL DEFAULT '{}'::jsonb,
  features JSONB NOT NULL DEFAULT '{}'::jsonb,
  explanation JSONB NOT NULL DEFAULT '[]'::jsonb,
  coverage JSONB NOT NULL DEFAULT '{}'::jsonb,
  calculated_at TIMESTAMPTZ NOT NULL,
  knowledge_cutoff TIMESTAMPTZ,
  input_snapshot_hash VARCHAR(64),
  input_source_hashes JSONB NOT NULL DEFAULT '[]'::jsonb,
  formula_hash VARCHAR(64),
  UNIQUE(stock_id, source_date, score_version, knowledge_cutoff)
);
CREATE INDEX IF NOT EXISTS ix_capital_scores_score_status ON capital_aware_scores(score, status);
CREATE INDEX IF NOT EXISTS ix_capital_scores_stock_date ON capital_aware_scores(stock_id, source_date);
