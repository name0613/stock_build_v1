# Security

- `.env` is ignored; `.env.example` contains only empty `FINMIND_API_TOKEN=` and `NAS_PASSWORD=`.
- FinMind token is read server-side only; it is never returned by an API, sent to the frontend, put in localStorage, written to raw metadata, or included in an image.
- NAS password is used only by `scripts/deploy_nas.py` to establish SSH. It is not placed in compose, Dockerfiles, Git or logs.
- PostgreSQL uses a Docker secret file; it is not LAN-published.
- API and worker use the public bridge only for outbound FinMind access; the API port remains unpublished and LAN ingress is still limited to nginx.
- Docker build contexts exclude `.env`, `secrets`, raw data, and reviewer bundles.
- Logs use sanitized error codes and never dump request parameters containing the token.
- Run `python scripts/secret_scan.py` before creating a reviewer bundle.
- Reviewer bundles are generated from an allowlist and contain only sanitized runtime evidence. If a secret scan fails, do not upload the bundle.
