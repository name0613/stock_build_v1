"""Generate source-bound, sanitized FinMind capability evidence on NAS."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.deploy_nas import remote  # noqa: E402


if __name__ == "__main__":
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(os.environ["NAS_HOST"], username=os.environ["NAS_USER"], password=os.environ["NAS_PASSWORD"], look_for_keys=False, allow_agent=False, timeout=15)
    try:
        output = remote(client, "cd /volume1/docker/tw-accumulation-evidence && docker compose exec -T worker env FINMIND_CAPABILITY_STDOUT=1 python /app/scripts/capability_probe.py", sudo=True)
    finally:
        client.close()
    evidence = json.loads(output)
    target = ROOT / "deployment_evidence/FINMIND_CAPABILITY_EVIDENCE.json"
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(target), "source_revision": evidence["source_revision"], "secret_included": False}, ensure_ascii=False))
