from __future__ import annotations

import json
import os
from pathlib import Path

import paramiko

from scripts.deploy_nas import remote


ROOT = Path(__file__).resolve().parents[1]


if __name__ == "__main__":
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(os.environ["NAS_HOST"], username=os.environ["NAS_USER"], password=os.environ["NAS_PASSWORD"], look_for_keys=False, allow_agent=False, timeout=15)
    try:
        source = (ROOT / "scripts/pit_postgres_evidence.py").read_text(encoding="utf-8")
        output = remote(client, "cd /volume1/docker/tw-accumulation-evidence && docker compose exec -T api env PIT_EVIDENCE_STDOUT=1 python -", sudo=True, input_text=source)
    finally:
        client.close()
    evidence = json.loads(output)
    evidence["secrets_included"] = False
    target = ROOT / "deployment_evidence/PIT_POSTGRES_EVIDENCE.json"
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(target), "historical_identical": evidence["historical_identical"], "later_cutoff_distinct": evidence["later_cutoff_distinct"], "secrets_included": False}, ensure_ascii=False))
