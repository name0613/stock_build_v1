# Operations

Worker schedules catch-up on startup, Monday-Friday 21:30 main sync and 23:00 retry. Dataset freshness is tracked separately; weekly holding data is not copied forward as a fake daily observation.

Inspect:

- `/api/data-status` for dataset state and job history.
- `/api/summary` for universe count and status counts.
- Parquet metadata sidecars for source parameters/date/fetch time/hash.
- `FINMIND_CAPABILITY_EVIDENCE.json` for actual capability probes.

Statuses are `RUNNING`, `SUCCESS`, `REUSED`, `PARTIAL`, `FAILED`; `REUSED` means the requested checkpoint keys were completely satisfied without a new physical provider request. Valid provider no-data is distinct from an unverified empty response; the latter is retryable. Coverage counters must reconcile requested, newly fetched, reused, valid no-data, retryable, permanent and physical requests. Error codes include `ACCESS_DENIED`, `RATE_LIMITED`, `UPSTREAM_5XX`, `TIMEOUT`, `SCHEMA_MISMATCH` and `RAW_STORAGE_UNAVAILABLE`.

Never run `docker system prune`, delete unknown volumes, or stop unrelated services. Broker checkpoint files make retries resumable and idempotent.
