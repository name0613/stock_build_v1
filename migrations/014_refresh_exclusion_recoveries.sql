CREATE TABLE IF NOT EXISTS refresh_exclusion_recoveries (
    job_id INTEGER PRIMARY KEY REFERENCES job_runs(id),
    stock_id VARCHAR(16) NOT NULL REFERENCES stocks(stock_id),
    requested_at TIMESTAMPTZ NOT NULL,
    previous_issue JSON NOT NULL,
    released_at TIMESTAMPTZ,
    result JSON NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_refresh_exclusion_recoveries_stock_id
    ON refresh_exclusion_recoveries(stock_id);
