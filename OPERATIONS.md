# Operations

Worker schedules catch-up on startup, Monday-Friday 21:30 main sync and 23:00 retry. The open-market 30-minute path refreshes only current-session institutional/shareholding/price sources; it does not launch the full 20-day broker catch-up or Score loop. Dataset freshness is tracked separately; weekly holding data is not copied forward as a fake daily observation.

Inspect:

- `/api/data-status` for dataset state and job history.
- `/api/summary` for universe count and status counts.
- `/api/finmind/quota` for the sanitized live provider allowance.
- `/api/favorites/fetch-and-score` for the durable score-ordered favorites refresh queue.
- Parquet metadata sidecars for source parameters/date/fetch time/hash.
- `FINMIND_CAPABILITY_EVIDENCE.json` for actual capability probes bound to the full deployed source revision and provider/dataset policy hashes.
- `BROKER_SOURCE_ISOLATION_EVIDENCE.json` for prohibited-row/revision counts, the database constraint, quarantine counts and authoritative rebuild state.

Statuses are `RUNNING`, `SUCCESS`, `REUSED`, `PARTIAL`, `FAILED`, `QUOTA_EXHAUSTED`, `WAITING_FOR_PROVIDER_PUBLICATION` and `SCORE_BLOCKED_BY_SOURCE_COVERAGE`; `REUSED` means every expected observation was previously verified without a new physical provider request. Valid provider no-data is distinct from an unverified empty response and is scoped to explicit observation dates; unreturned sessions, partial ranges and incomplete pagination are retryable. Broker retries first read the authenticated provider quota, preserve the configurable `BROKER_QUOTA_RESERVE`, select only the usable pending budget, and persist `next_eligible_retry_at` with classified backoff. Weekly holding publication waits are persisted with the target date, last provider check, check result, query type and next eligible check. A wait is throttled for full-market requests, but a deterministic single-stock canary revalidates it; observed publication invalidates stale wait knowledge and moves to `HOLDING_PUBLICATION_PARTIAL` until every stock passes all 15 canonical buckets. Global source coverage is advisory for display: each stock is evaluated independently, and list/ranking/detail surfaces show its latest persisted numeric Score while stocks without a valid point-in-time input contract remain `DATA_INSUFFICIENT`. Coverage counters must reconcile expected, verified, unresolved, newly fetched, reused, valid no-data, retryable, permanent and physical requests. Error codes include `ACCESS_DENIED`, `RATE_LIMITED`, `UPSTREAM_5XX`, `TIMEOUT`, `SCHEMA_MISMATCH`, `INCOMPLETE_PROVIDER_COVERAGE`, `HOLDING_BUCKETS_INCOMPLETE`, `HOLDING_PUBLICATION_PARTIAL`, `WAITING_FOR_PROVIDER_PUBLICATION` and `RAW_STORAGE_UNAVAILABLE`.

User-triggered favorites refreshes add `QUEUED`, `WAITING_FOR_QUOTA`, and `WAITING_FOR_PROVIDER`. Their parent `JobRun` preserves the original score-descending stock order, completed stock IDs, per-dataset completion, current stock, quota snapshot, and next retry time. Worker restarts return an interrupted favorites job to `QUEUED` and resume it; scheduled full/intraday sync and favorites refresh share a provider-work lock so they do not spend quota concurrently inside the worker.

Never run `docker system prune`, delete unknown volumes, or stop unrelated services. Broker checkpoint files make retries resumable and idempotent.
