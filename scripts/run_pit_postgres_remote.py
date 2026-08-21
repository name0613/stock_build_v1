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
        source = (ROOT / "scripts/pit_postgres_evidence.py").read_text(encoding="utf-8")
        output = remote(client, "cd /volume1/docker/tw-accumulation-evidence && docker compose exec -T api env PIT_EVIDENCE_STDOUT=1 python -", sudo=True, input_text=source)
    finally:
        client.close()
    evidence = json.loads(output)
    evidence["secrets_included"] = False
    target = ROOT / "deployment_evidence/PIT_POSTGRES_EVIDENCE.json"
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(target), "historical_bit_for_bit_identical": evidence["historical_bit_for_bit_identical"], "later_calculation_distinct": evidence["later_calculation_distinct"], "non_null_initial_score": evidence["non_null_initial_score"], "secrets_included": False}, ensure_ascii=False))
