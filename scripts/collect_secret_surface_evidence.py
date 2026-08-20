"""Scan local and NAS runtime surfaces without persisting secret values."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.deploy_nas import remote
from scripts.secret_scan import PATTERNS, scan


def scan_text(text: str, surface: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for name, pattern in PATTERNS.items():
        if pattern.search(text):
            findings.append({"rule": name, "surface": surface})
    return findings


def remote_surfaces() -> list[dict[str, object]]:
    host = os.getenv("NAS_HOST")
    user = os.getenv("NAS_USER")
    password = os.getenv("NAS_PASSWORD")
    if not host or not user or not password:
        return [{"surface": "NAS runtime", "status": "SKIPPED", "reason": "NAS credentials not provided to evidence process"}]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password, look_for_keys=False, allow_agent=False, timeout=15)
    try:
        commands = {
            "container_inspect": "cd /volume1/docker/tw-accumulation-evidence && docker compose ps -q api worker | xargs -r docker inspect",
            "image_inspect": "cd /volume1/docker/tw-accumulation-evidence && docker compose images -q api worker | sort -u | xargs -r docker image inspect",
            "container_logs": "cd /volume1/docker/tw-accumulation-evidence && docker compose logs --no-color --tail=200 api worker",
            "compose_config": "cd /volume1/docker/tw-accumulation-evidence && docker compose config",
        }
        results: list[dict[str, object]] = []
        for surface, command in commands.items():
            output = remote(client, command, sudo=True, check=False)
            findings = scan_text(output, f"NAS:{surface}")
            results.append({"surface": f"NAS:{surface}", "status": "FAIL" if findings else "PASS", "findings": findings})
        return results
    finally:
        client.close()


if __name__ == "__main__":
    extras = [ROOT / name for name in ("frontend/dist", "deployment_evidence", "screenshots", "sanitized_sample_data") if (ROOT / name).exists()]
    local = scan(extras)
    surfaces = [{"surface": "tracked_worktree_dist_evidence", "status": local["status"], "scanned_files": local["scanned_files"], "findings": [{"rule": item["rule"], "surface": item["path"]} for item in local["findings"]]}]
    surfaces.extend(remote_surfaces())
    result = {"generated_at": datetime.now(timezone.utc).isoformat(), "status": "PASS" if all(item["status"] in {"PASS", "SKIPPED"} for item in surfaces) else "FAIL", "secret_values_redacted": True, "surfaces": surfaces}
    output = ROOT / "deployment_evidence/SECRET_SURFACE_EVIDENCE.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(output), "status": result["status"], "secret_values_included": False}, ensure_ascii=False))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
