# Architecture

```text
FinMind API -> bounded worker -> raw Parquet on NAS
                         \-> normalized PostgreSQL -> precomputed features/scores
nginx -> React/Vite frontend -> FastAPI -> PostgreSQL
```

`PriceDaily` preserves FinMind `Trading_money` and `Trading_turnover` as
nullable provider fields. Raw Parquet replay uses the sanitized raw fetch
timestamp and writes the same source revision/upsert path; it never derives
Trading_money from close and volume. `capital_aware_scores` stores the
versioned `capital-aware-v7` snapshot beside immutable `s-only-v6` rows.

The homepage has three whitelisted rankings: `stealth` (v6), `large_capital`
and `high_confidence` (v7), with high confidence as the default view. v7 uses
fixed TWD breakpoints and independent source families, not a daily universe
percentile or a sum of potentially overlapping flow estimates.

Services are `postgres`, `api`, `worker`, `frontend`, `nginx`. The Compose network is internal; nginx is the only LAN-facing port. `postgres_data` and `raw_data` are persistent named volumes. All ingestion paths are idempotent UPSERTs keyed by `(stock_id, source_date[, broker])`.

The database stores `source_date`, `fetched_at`, `calculated_at`, and `score_version` separately. Raw broker rows stay in date-partitioned Parquet; PostgreSQL stores normalized rows, aggregation, query-ready features, explanations and health evidence.

Score calculation is pure Python and deterministic. The current release uses configuration `s-only-v6`; it has no black-box ML. The API only sorts a whitelist of columns and uses SQLAlchemy parameters for filters. The complete score specification and formula hash are exposed by `/api/score-spec` and persisted in `score_versions`. v6 publishes a current mixed snapshot only when a score `JobRun` is bound to the current target date, formula, calendar, authoritative source-state hash, evaluated stock count and immutable score-snapshot hash. Numeric rows are produced only for stocks that pass the per-stock readiness contract; other stocks are persisted as `DATA_INSUFFICIENT`. A failed or stale score run therefore cannot expose an older numeric snapshot; prior score versions remain historical and provenance-bound.

Each source and broker dataset has a durable incremental checkpoint. Daily source checkpoints enumerate versioned Taiwan trading sessions; holding-distribution checkpoints assign a source observation within ±4 days to exactly one Friday period. Coverage is granted only to exact returned-and-accepted observations or exact dates with validated provider no-data semantics. Partial ranges and incomplete pagination remain retryable across restart. Non-broker work uses a durable round-robin stock cursor, so a finite quota does not repeatedly favor low stock IDs. A run separately records physically received, accepted, rejected and versioned rows plus checkpoint-reused observations; impossible counter combinations are rejected. `SUCCESS` and `REUSED` require complete verified coverage.

Broker production scoring is currently bound to `finmind-observed-stock-session-row-v1`: only directly observed rows whose stock, session, branch, buy and sell fields validate can contribute. Omitted branches, empty reports and unproven report completeness remain `unknown`, are never imputed as zero, and keep concentration/spike metrics unavailable. A dedicated provider completeness contract may be promoted only after independent live evidence proves it. Holding completeness is bound to `finmind-holding-shares-level-v1`: all 15 canonical thresholds must occur exactly once with non-null percent, people and shares; unknown, duplicate or missing buckets fail closed.

Capability probes are structurally outside ingestion. In particular, `TaiwanStockTradingDailyReportSecIdAgg` cannot enter `BrokerDaily`; ORM and PostgreSQL constraints require the official dataset, and scoring, API and provenance queries independently filter by source. Migration 007 quarantines any pre-constraint rows/revisions, invalidates affected v4 features/scores, and keeps an affected stock pending until 20 official broker sessions and a regenerated score are present.

## Failure boundaries

- 401/403/429/5xx/timeout/invalid JSON/schema drift are explicit error codes.
- 403 becomes `ACCESS_DENIED`, not an empty dataset.
- missing S-level data makes score `NULL` and status `DATA_INSUFFICIENT`.
- global source `PARTIAL`/quota status remains observable but is not a universal score veto; readiness is evaluated independently for every eligible stock and `/api/readiness` exposes the same audit.
- holding distribution is low-frequency; the UI shows source date and latest available date.
- no official stable 5%+ automated source is assumed; its table remains empty and unavailable is documented.
