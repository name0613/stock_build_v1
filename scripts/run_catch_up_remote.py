from __future__ import annotations

import json
import os
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "/volume1/docker/tw-accumulation-evidence"


def main() -> None:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(os.environ.get("NAS_HOST", "192.168.31.138"), username=os.environ["NAS_USER"], password=os.environ["NAS_PASSWORD"], look_for_keys=False, allow_agent=False, timeout=15)
    try:
        source = (ROOT / "scripts/catch_up_once_evidence.py").read_text(encoding="utf-8")
        command = f"cd {PROJECT} && sudo -S -p '' docker compose exec -T worker env CATCH_UP_EVIDENCE_STDOUT=1 python -"
        stdin, stdout, stderr = client.exec_command(command)
        stdin.write(os.environ["NAS_PASSWORD"] + "\n")
        stdin.write(source)
        stdin.flush()
        stdin.channel.shutdown_write()
        output = stdout.read().decode("utf-8", "replace").strip()
        error = stderr.read().decode("utf-8", "replace")
        if stdout.channel.recv_exit_status() != 0:
            raise RuntimeError(f"remote catch-up failed without credential output: {error[:500]}")
    finally:
        client.close()
    evidence = json.loads(output)
    evidence["secrets_included"] = False
    target = ROOT / "deployment_evidence/CATCH_UP_ATTEMPT_EVIDENCE.json"
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    result = evidence.get("result") or {}
    print(json.dumps({"path": str(target), "status": result.get("status"), "fatal_code": result.get("fatal_code"), "secrets_included": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
