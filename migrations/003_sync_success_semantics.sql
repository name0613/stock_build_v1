ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS last_http_success_at TIMESTAMPTZ;
ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS last_fully_successful_sync TIMESTAMPTZ;
ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS last_usable_data_at TIMESTAMPTZ;
