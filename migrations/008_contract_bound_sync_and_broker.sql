ALTER TABLE broker_daily
  ADD COLUMN IF NOT EXISTS provider_contract_version VARCHAR(100);

UPDATE broker_daily
SET provider_report_complete = FALSE,
    provider_contract_version = NULL
WHERE provider_contract_version IS NULL;

CREATE INDEX IF NOT EXISTS ix_broker_stock_dataset_date
  ON broker_daily(stock_id, source_dataset, source_date);

ALTER TABLE data_sync_status
  ADD COLUMN IF NOT EXISTS observations_reused_this_attempt INTEGER NOT NULL DEFAULT 0;
