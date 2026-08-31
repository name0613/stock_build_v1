# Deployment runbook

## Preconditions

1. Provide `NAS_HOST=192.168.31.138`, `NAS_USER`, `NAS_PASSWORD` and `FINMIND_API_TOKEN` through the execution environment. Never put them in source, Git, a screenshot, a log or a reviewer bundle.
2. Run `python scripts/deploy_nas.py`. It performs read-only NAS preflight before creating an isolated project directory.
3. If Docker/Compose is absent, stop. Install or enable the NAS container runtime outside this repository; do not alter unrelated services.

Production FinMind credentials are written only to `secrets/finmind_api_token` and mounted with Compose secrets. The application reads `FINMIND_API_TOKEN_FILE`; production `.env`, image ENV, frontend assets and logs do not contain the token. The worker exposes an internal health endpoint on port 8001 and the API exposes sanitized `/api/worker-health` heartbeat state.

## Runtime verification

```text
docker compose ps
curl http://192.168.31.138:<PORT>/health
curl 'http://192.168.31.138:<PORT>/api/summary'
```

Then restart `worker`, `api`, and `frontend` one at a time, re-check health and database row counts. Verify PostgreSQL is not published to LAN. Confirm the actual port in the sanitized deployment evidence.

For the capital-aware release also verify `/api/score-spec` contains
`capital-aware-v7` and its 64-character formula hash, then call
`/api/rankings?kind=stealth`, `?kind=large_capital` and
`?kind=high_confidence`. Confirm the latter two return v7 metadata and that a
stock without formal Trading_money is `DATA_INSUFFICIENT`, never a zero-valued
capital signal. Repeat these checks after each service restart.

## Port selection

Default is `18080`. Preflight lists current listeners. If occupied, set `WEB_PORT` to a verified unused port and record only the final URL, never credentials.
