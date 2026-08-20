# Verification summary

This file records the reproducible checks used for the reviewer bundle.

- Backend: `.venv\\Scripts\\python.exe -m pytest backend/tests -q --junitxml=test_results/backend-junit.xml` — 39 passed, including scheduled multi-stock catch-up, point-in-time revision safety, actual holding bucket boundaries, broker concentration/window semantics, partial revision merge, worker health, quota/application failure injection, and holding adjustment metadata handling.
- Static checks: `.venv\\Scripts\\python.exe -m ruff check backend/app backend/tests scripts` — passed.
- Frontend production build: `npm run build` from `frontend/` — passed.
- Browser E2E against NAS: `E2E_BASE_URL=http://192.168.31.138:18080 npx playwright test` from `frontend/` — 4 passed, including detail provenance/charts, filters, rankings, freshness, and score hash.
- Secret scan: `.venv\\Scripts\\python.exe scripts/secret_scan.py` — passed; structural rules scan source, evidence and generated artifacts without exposing finding values.
- Runtime evidence: `deployment_evidence/NAS_DEPLOYMENT_EVIDENCE.json` contains sanitized health, summary, data-status, API p95 benchmarks and `secrets_included: false`.
- NAS persistence evidence: `deployment_evidence/NAS_COMPOSE_PERSISTENCE_EVIDENCE.json` records Compose validation, service/container/image/network/volume state and before/after PostgreSQL checksums across a recreate.
- NAS source binding: `deployment_evidence/NAS_RUNTIME_METADATA_EVIDENCE.json` verifies the deployed source revision matches the local Git revision and that the PostgreSQL/raw named volumes are present.
- Current provider state: the runtime evidence records FinMind `QUOTA_EXHAUSTED` as an explicit fail-closed partial state; no incomplete source is presented as a successful full catch-up.

The repository does not have a local Docker daemon; Docker Compose verification was performed on the target NAS and is documented in `deployment_evidence/NAS_DEPLOYMENT_EVIDENCE.*`.
