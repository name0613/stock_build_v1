-- Versioned runtime hardening migration.  It is intentionally idempotent so a
-- deployment can resume after a process interruption, while SQL failures are
-- allowed to abort startup in db.py.
ALTER TABLE accumulation_features ADD COLUMN IF NOT EXISTS knowledge_cutoff TIMESTAMPTZ;
ALTER TABLE accumulation_features ADD COLUMN IF NOT EXISTS input_snapshot_hash VARCHAR(64);
ALTER TABLE accumulation_scores ADD COLUMN IF NOT EXISTS knowledge_cutoff TIMESTAMPTZ;
ALTER TABLE accumulation_scores ADD COLUMN IF NOT EXISTS input_snapshot_hash VARCHAR(64);
ALTER TABLE accumulation_scores ADD COLUMN IF NOT EXISTS input_source_hashes JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE accumulation_scores ADD COLUMN IF NOT EXISTS formula_hash VARCHAR(64);
ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ;
ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS last_fetch_at TIMESTAMPTZ;
ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS usable_records INTEGER NOT NULL DEFAULT 0;
ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS stored_records INTEGER NOT NULL DEFAULT 0;
ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS staleness_state VARCHAR(32);
ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS attempt_latest_source_date DATE;
ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS expected_latest_source_date DATE;
ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS source_age_days INTEGER;
ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS rows_received_this_attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS rows_accepted_this_attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS rows_rejected_this_attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS rows_versioned_this_attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS stored_rows_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS requested_start_date DATE;
ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS requested_end_date DATE;
ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS error_code VARCHAR(64);
ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS stocks_attempted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS stocks_completed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS stocks_failed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS checkpoint_state JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE score_versions ADD COLUMN IF NOT EXISTS manifest_hash VARCHAR(64);
CREATE TABLE IF NOT EXISTS source_revisions (
  id BIGSERIAL PRIMARY KEY,
  dataset VARCHAR(100) NOT NULL,
  stock_id VARCHAR(16) REFERENCES stocks(stock_id),
  source_date DATE,
  natural_key VARCHAR(255) NOT NULL,
  payload JSONB NOT NULL,
  content_hash VARCHAR(64) NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT uq_source_revision_content UNIQUE(dataset, natural_key, content_hash)
);
CREATE INDEX IF NOT EXISTS ix_source_revisions_dataset ON source_revisions(dataset);
CREATE INDEX IF NOT EXISTS ix_source_revisions_stock_id ON source_revisions(stock_id);
CREATE INDEX IF NOT EXISTS ix_source_revisions_source_date ON source_revisions(source_date);
CREATE INDEX IF NOT EXISTS ix_source_revisions_fetched_at ON source_revisions(fetched_at);
ALTER TABLE accumulation_features DROP CONSTRAINT IF EXISTS uq_features_stock_date;
ALTER TABLE accumulation_scores DROP CONSTRAINT IF EXISTS uq_score_stock_date_version;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_features_stock_date_cutoff') THEN
    ALTER TABLE accumulation_features ADD CONSTRAINT uq_features_stock_date_cutoff UNIQUE(stock_id, source_date, knowledge_cutoff);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_score_stock_date_version_cutoff') THEN
    ALTER TABLE accumulation_scores ADD CONSTRAINT uq_score_stock_date_version_cutoff UNIQUE(stock_id, source_date, score_version, knowledge_cutoff);
  END IF;
END $$;
