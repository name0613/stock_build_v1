"""Run the PostgreSQL legacy backfill probe inside the NAS worker image."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import paramiko  # noqa: E402

from scripts.deploy_nas import remote  # noqa: E402


PROJECT = "/volume1/docker/tw-accumulation-evidence"


def main() -> None:
    host = os.getenv("NAS_HOST")
    user = os.getenv("NAS_USER")
    password = os.getenv("NAS_PASSWORD")
    if not host or not user or not password:
        raise SystemExit("NAS_HOST, NAS_USER, and NAS_PASSWORD are required through the environment")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password, look_for_keys=False, allow_agent=False, timeout=15)
    try:
        output = remote(client, f"cd {PROJECT} && docker compose exec -T worker python /app/scripts/legacy_migration_evidence.py", sudo=True)
    finally:
        client.close()
    # The inner script writes the sanitized JSON into the mounted worktree;
    # return only the final summary line so no database payload can leak.
    line = next((item for item in reversed(output.splitlines()) if item.strip().startswith("{")), "{}")
    result = json.loads(line)
    print(json.dumps({key: value for key, value in result.items() if key not in {"temporary_schema"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
