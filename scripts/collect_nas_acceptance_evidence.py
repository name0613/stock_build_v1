from __future__ import annotations

import json
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path

import paramiko


HOST = os.getenv("NAS_HOST", "192.168.31.138")
USER = os.getenv("NAS_USER")
PASSWORD = os.getenv("NAS_PASSWORD")
PROJECT = "/volume1/docker/tw-accumulation-evidence"


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
        compose = run(ssh, f"cd {PROJECT} && docker compose config --quiet && docker compose config --services")
        ps = run(ssh, f"cd {PROJECT} && docker compose ps --format json")
        containers = run(ssh, "docker ps --filter label=com.docker.compose.project=tw-accumulation-evidence --format '{{.ID}} {{.Names}} {{.Image}} {{.Status}} {{.Ports}}'")
        images = run(ssh, "docker ps --filter label=com.docker.compose.project=tw-accumulation-evidence --format '{{.Image}}' | sort -u | xargs -r docker image inspect --format '{{.RepoTags}} {{.Id}}'")
        volumes = run(ssh, "docker volume inspect tw-accumulation-evidence_postgres_data tw-accumulation-evidence_raw_data --format '{{.Name}} {{.Mountpoint}}'")
        networks = run(ssh, "docker network inspect tw-accumulation-evidence_internal tw-accumulation-evidence_public --format '{{.Name}} {{range .Containers}}{{.Name}} {{end}}'")
        before = run(ssh, f"cd {PROJECT} && docker compose exec -T postgres psql -U accumulation -d accumulation -Atc \"SELECT 'stocks',count(*),md5(coalesce(string_agg(stock_id||':'||is_common_stock::text,',' order by stock_id),'')) FROM stocks UNION ALL SELECT 'scores',count(*),md5(coalesce(string_agg(stock_id||':'||source_date::text||':'||status,',' order by stock_id,source_date),'')) FROM accumulation_scores;\"")
        run(ssh, f"cd {PROJECT} && docker compose up -d --force-recreate postgres")
        run(ssh, f"cd {PROJECT} && for i in $(seq 1 30); do docker compose exec -T postgres pg_isready -U accumulation -d accumulation >/dev/null 2>&1 && break; sleep 2; done")
        after = run(ssh, f"cd {PROJECT} && docker compose exec -T postgres psql -U accumulation -d accumulation -Atc \"SELECT 'stocks',count(*),md5(coalesce(string_agg(stock_id||':'||is_common_stock::text,',' order by stock_id),'')) FROM stocks UNION ALL SELECT 'scores',count(*),md5(coalesce(string_agg(stock_id||':'||source_date::text||':'||status,',' order by stock_id,source_date),'')) FROM accumulation_scores;\"")
        evidence = {"generated_at": datetime.now(timezone.utc).isoformat(), "host": HOST, "project": PROJECT, "compose_config_valid": True, "compose_services": compose.splitlines(), "compose_ps": ps, "containers": containers.splitlines(), "images": images.splitlines(), "volumes": volumes.splitlines(), "networks": networks.splitlines(), "persistence": {"before": before.splitlines(), "after": after.splitlines(), "identical": before == after, "postgres_recreated_without_volume_delete": True}, "secrets_included": False, "sanitized": True}
        output = Path("deployment_evidence")
        output.mkdir(exist_ok=True)
        (output / "NAS_COMPOSE_PERSISTENCE_EVIDENCE.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"path": str(output / "NAS_COMPOSE_PERSISTENCE_EVIDENCE.json"), "persistence_identical": evidence["persistence"]["identical"], "secrets_included": False}, ensure_ascii=False))
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
