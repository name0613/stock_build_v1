# Architecture

```text
FinMind API -> bounded worker -> raw Parquet on NAS
                         \-> normalized PostgreSQL -> precomputed features/scores
nginx -> React/Vite frontend -> FastAPI -> PostgreSQL
```

Services are `postgres`, `api`, `worker`, `frontend`, `nginx`. The Compose network is internal; nginx is the only LAN-facing port. `postgres_data` and `raw_data` are persistent named volumes. All ingestion paths are idempotent UPSERTs keyed by `(stock_id, source_date[, broker])`.

The database stores `source_date`, `fetched_at`, `calculated_at`, and `score_version` separately. Raw broker rows stay in date-partitioned Parquet; PostgreSQL stores normalized rows, aggregation, query-ready features, explanations and health evidence.

Score calculation is pure Python and deterministic. The current release uses configuration `s-only-v4`; it has no black-box ML. The API only sorts a whitelist of columns and uses SQLAlchemy parameters for filters. The complete score specification and formula hash are exposed by `/api/score-spec` and persisted in `score_versions`. v4 fail-closes explicit null broker rows and gates all current score surfaces on authoritative current provider status; prior score versions remain historical and provenance-bound.

Each source and broker dataset has a durable incremental checkpoint. A run records requested keys, newly fetched keys, reused complete keys, valid provider no-data, retryable/permanent failures and physical provider requests. Reuse is a successful terminal state; an unverified empty provider response remains retryable. Broker features require the sanitized provider completeness marker, and holding features require both relevant normalized bucket boundaries with valid numeric fields.

## Failure boundaries

- 401/403/429/5xx/timeout/invalid JSON/schema drift are explicit error codes.
- 403 becomes `ACCESS_DENIED`, not an empty dataset.
- missing S-level data makes score `NULL` and status `DATA_INSUFFICIENT`.
- holding distribution is low-frequency; the UI shows source date and latest available date.
- no official stable 5%+ automated source is assumed; its table remains empty and unavailable is documented.
