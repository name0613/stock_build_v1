CREATE TABLE IF NOT EXISTS stock_refresh_issues (
  stock_id VARCHAR(16) PRIMARY KEY REFERENCES stocks(stock_id),
  no_data_attempts INTEGER NOT NULL DEFAULT 0,
  status VARCHAR(32) NOT NULL,
  reason_code VARCHAR(64) NOT NULL,
  first_attempt_at TIMESTAMPTZ NOT NULL,
  last_attempt_at TIMESTAMPTZ NOT NULL,
  last_job_id INTEGER REFERENCES job_runs(id),
  details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_stock_refresh_issues_status
  ON stock_refresh_issues(status);

CREATE INDEX IF NOT EXISTS ix_stock_refresh_issues_last_attempt_at
  ON stock_refresh_issues(last_attempt_at);
