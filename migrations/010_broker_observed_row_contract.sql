ALTER TABLE broker_daily
  ADD COLUMN IF NOT EXISTS provider_row_validated BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS provider_row_contract_version VARCHAR(100);

-- The former completeness flag was based on response shape, not independent
-- evidence that every branch was returned. Preserve the historical columns for
-- audit only; do not promote those rows into the v6 observed-row contract.
UPDATE broker_daily
SET provider_row_validated = FALSE,
    provider_row_contract_version = NULL
WHERE provider_row_contract_version IS NULL;
