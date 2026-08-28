ALTER TABLE stocks
  ADD COLUMN IF NOT EXISTS is_favorite BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS ix_stocks_is_favorite
  ON stocks(is_favorite);
