"""Capture sanitized NAS source/image/volume binding evidence."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import paramiko


HOST = os.getenv("NAS_HOST", "192.168.31.138")
USER = os.getenv("NAS_USER")
PASSWORD = os.getenv("NAS_PASSWORD")
PROJECT = "/volume1/docker/tw-accumulation-evidence"
ROOT = Path(__file__).resolve().parents[1]


def run(ssh: paramiko.SSHClient, command: str) -> str:
    wrapped = f"sudo -S -p '' sh -c {shlex.quote(command)}"
    stdin, stdout, stderr = ssh.exec_command(wrapped)
    stdin.write((PASSWORD or "") + "\n")
    stdin.flush()
    stdin.channel.shutdown_write()
    output = stdout.read().decode("utf-8", "replace")
    if stdout.channel.recv_exit_status() != 0:
        raise RuntimeError(stderr.read().decode("utf-8", "replace")[:500])
    return output.strip()


def main() -> None:
    if not USER or not PASSWORD:
        raise SystemExit("NAS_USER and NAS_PASSWORD are required through the environment")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, look_for_keys=False, allow_agent=False, timeout=15)
    try:
        revision = run(ssh, f"cat {PROJECT}/DEPLOYED_SOURCE_REVISION")
        services = run(ssh, f"cd {PROJECT} && docker compose config --images")
        containers = run(ssh, "docker ps --filter label=com.docker.compose.project=tw-accumulation-evidence --format '{{.Names}} {{.Image}} {{.Status}}'")
        images = run(ssh, "docker ps --filter label=com.docker.compose.project=tw-accumulation-evidence --format '{{.Image}}' | sort -u | xargs -r docker image inspect --format '{{.RepoTags}} {{.Id}}'")
        volumes = run(ssh, "docker volume inspect tw-accumulation-evidence_postgres_data tw-accumulation-evidence_raw_data --format '{{.Name}} {{.Mountpoint}}'")
    finally:
        ssh.close()
    local_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    evidence = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": HOST,
        "project": PROJECT,
        "local_source_revision": local_revision,
        "deployed_source_revision": revision,
        "source_revision_matches": revision == local_revision,
        "compose_images": services.splitlines(),
        "running_containers": containers.splitlines(),
        "image_ids": images.splitlines(),
        "named_volumes": volumes.splitlines(),
        "required_named_volumes_present": "tw-accumulation-evidence_postgres_data" in volumes and "tw-accumulation-evidence_raw_data" in volumes,
        "sanitized": True,
        "secrets_included": False,
    }
    output = Path("deployment_evidence")
    output.mkdir(exist_ok=True)
    path = output / "NAS_RUNTIME_METADATA_EVIDENCE.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(path), "source_revision_matches": evidence["source_revision_matches"], "secrets_included": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
