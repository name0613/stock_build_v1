ALTER TABLE data_sync_status
  ADD COLUMN IF NOT EXISTS physical_requests_this_attempt INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS counter_attempt_id VARCHAR(64),
  ADD COLUMN IF NOT EXISTS counter_semantics_version VARCHAR(64) NOT NULL DEFAULT 'legacy-pre-v5-unversioned',
  ADD COLUMN IF NOT EXISTS counters_are_current_attempt BOOLEAN NOT NULL DEFAULT FALSE;

-- Some legacy databases were created by SQLAlchemy before 001_init was
-- recorded and therefore have JSON rather than the intended JSONB column.
-- Normalize the storage type before using jsonb operators; the cast preserves
-- every existing object value.
ALTER TABLE data_sync_status
  ALTER COLUMN metadata_json TYPE JSONB
  USING COALESCE(metadata_json::jsonb, '{}'::jsonb);

UPDATE data_sync_status
SET metadata_json = COALESCE(metadata_json, '{}'::jsonb) || jsonb_build_object(
      'legacy_pre_v5_counter_snapshot',
      jsonb_build_object(
        'records', COALESCE(records, 0),
        'rows_received', COALESCE(rows_received_this_attempt, 0),
        'rows_accepted', COALESCE(rows_accepted_this_attempt, 0),
        'rows_rejected', COALESCE(rows_rejected_this_attempt, 0),
        'rows_versioned', COALESCE(rows_versioned_this_attempt, 0),
        'observations_reused', COALESCE(observations_reused_this_attempt, 0),
        'stored_rows_total', COALESCE(stored_rows_total, 0),
        'semantics', 'legacy-pre-v5-unversioned',
        'current_attempt', false
      )
    ),
    physical_requests_this_attempt = 0,
    rows_received_this_attempt = 0,
    rows_accepted_this_attempt = 0,
    rows_rejected_this_attempt = 0,
    rows_versioned_this_attempt = 0,
    observations_reused_this_attempt = 0,
    counter_attempt_id = NULL,
    counter_semantics_version = 'legacy-pre-v5-reset-v1',
    counters_are_current_attempt = FALSE
WHERE counter_semantics_version = 'legacy-pre-v5-unversioned';
