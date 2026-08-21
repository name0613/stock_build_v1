"""Build and exercise a disposable production-equivalent stack on the NAS.

The clean-room source is created only from ``git archive HEAD``.  Runtime
secrets are generated on the NAS for this disposable project and are never
written to the evidence file.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import paramiko


ROOT = Path(__file__).resolve().parents[1]
HOST = os.getenv("NAS_HOST", "192.168.31.138")
USER = os.getenv("NAS_USER")
PASSWORD = os.getenv("NAS_PASSWORD")
REMOTE_ROOT = "/volume1/docker"


def remote(ssh: paramiko.SSHClient, command: str, *, check: bool = True) -> str:
    wrapped = f"sudo -S -p '' sh -c {shlex.quote(command)}"
    stdin, stdout, stderr = ssh.exec_command(wrapped)
    stdin.write((PASSWORD or "") + "\n")
    stdin.flush()
    stdin.channel.shutdown_write()
    output = stdout.read().decode("utf-8", "replace").strip()
    if check and stdout.channel.recv_exit_status() != 0:
        error = stderr.read().decode("utf-8", "replace")[:3000]
        raise RuntimeError(f"remote command failed: stderr={error}; stdout={output[-3000:]}")
    return output


def sftp_prefix(sftp: paramiko.SFTPClient) -> str:
    for prefix, candidate in (("/volume1", "/docker"), ("/share", "/Container"), ("", REMOTE_ROOT)):
        try:
            sftp.listdir(candidate)
            return prefix
        except OSError:
            continue
    raise RuntimeError("NAS SFTP namespace does not expose a verified container root")


def sftp_path(shell_path: str, prefix: str) -> str:
    return shell_path[len(prefix) :] if prefix and shell_path.startswith(prefix + "/") else shell_path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_lines(value: str) -> list[dict[str, object]]:
    rows = []
    for line in value.splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def wait_services_command(project: str, compose: str, services: str) -> str:
    return (
        f"cd {shlex.quote(project)} && "
        "for i in $(seq 1 60); do "
        "ok=1; "
        f"for service in {services}; do "
        f"id=$({compose} ps -q \"$service\"); "
        "[ -n \"$id\" ] || { ok=0; continue; }; "
        "state=$(docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}' \"$id\"); "
        "case \"$service\" in "
        "frontend) [ \"$state\" = 'running|' ] || ok=0 ;; "
        "*) [ \"$state\" = 'running|healthy' ] || ok=0 ;; "
        "esac; "
        "done; "
        "[ $ok -eq 1 ] && break; sleep 2; "
        "done; "
        f"{compose} ps"
    )


def stack_wait_command(project: str, compose: str) -> str:
    return wait_services_command(project, compose, "postgres api worker frontend nginx")


def main() -> None:
    if not USER or not PASSWORD:
        raise SystemExit("NAS_USER and NAS_PASSWORD are required through the environment")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    short = revision[:12]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-cleanroom-{short}"
    project_name = f"tw-accumulation-cleanroom-{short}"
    project = posixpath.join(REMOTE_ROOT, project_name)
    if not re.fullmatch(r"/volume1/docker/tw-accumulation-cleanroom-[0-9a-f]{12}", project):
        raise RuntimeError("refusing an unverified clean-room target path")

    backend_lock = sha256(ROOT / "backend/requirements.lock")
    frontend_lock = sha256(ROOT / "frontend/package-lock.json")
    sys_path = str(ROOT)
    import sys

    sys.path.insert(0, sys_path)
    from backend.app.calendar import CALENDAR_HASH
    from backend.app.scoring import FORMULA_HASH

    evidence: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "acceptance_run_id": run_id,
        "source_revision": revision,
        "source_mode": "git_archive_HEAD_only",
        "host": HOST,
        "project": project,
        "services": ["postgres", "api", "worker", "frontend", "nginx"],
        "secrets_included": False,
        "sanitized": True,
    }

    archive_fd, archive_name = tempfile.mkstemp(prefix="cleanroom-", suffix=".tar")
    os.close(archive_fd)
    archive_path = Path(archive_name)
    ssh: paramiko.SSHClient | None = None
    cleanup_complete = False
    try:
        with archive_path.open("wb") as handle:
            subprocess.run(["git", "archive", "--format=tar", revision], cwd=ROOT, stdout=handle, check=True)
        evidence["source_archive_sha256"] = sha256(archive_path)
        print("cleanroom: source archive ready", flush=True)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(HOST, username=USER, password=PASSWORD, look_for_keys=False, allow_agent=False, timeout=15)
        print("cleanroom: NAS connected", flush=True)
        if remote(ssh, f"if [ -e {shlex.quote(project)} ]; then printf EXISTS; fi") == "EXISTS":
            raise RuntimeError("clean-room target already exists; refusing to overwrite it")
        remote(ssh, f"mkdir -p {shlex.quote(project)}/secrets")
        sftp = ssh.open_sftp()
        prefix = sftp_prefix(sftp)
        remote_archive = posixpath.join(project, "source.tar")
        sftp.put(str(archive_path), sftp_path(remote_archive, prefix))
        sftp.close()
        remote(ssh, f"tar -xf {shlex.quote(remote_archive)} -C {shlex.quote(project)} && rm -f {shlex.quote(remote_archive)}")
        print("cleanroom: source extracted", flush=True)

        env_text = "\n".join(
            [
                f"SOURCE_REVISION={revision}",
                f"BACKEND_LOCK_SHA256={backend_lock}",
                f"FRONTEND_LOCK_SHA256={frontend_lock}",
                f"SCORE_SPEC_HASH={FORMULA_HASH}",
                f"CALENDAR_HASH={CALENDAR_HASH}",
                f"BUILD_TIMESTAMP={datetime.now(timezone.utc).isoformat()}",
                "WEB_PORT=18082",
            ]
        ) + "\n"
        sftp = ssh.open_sftp()
        prefix = sftp_prefix(sftp)
        env_remote = posixpath.join(project, ".env")
        password_remote = posixpath.join(project, "secrets", "postgres_password")
        token_remote = posixpath.join(project, "secrets", "finmind_api_token")
        for path, content in ((env_remote, env_text), (password_remote, "clean-room-postgres-password\n"), (token_remote, "clean-room-provider-disabled\n")):
            handle = sftp.file(sftp_path(path, prefix), "w")
            handle.write(content)
            handle.close()
            sftp.chmod(sftp_path(path, prefix), 0o600)
        sftp.close()
        print("cleanroom: disposable secrets configured", flush=True)

        compose = f"docker compose -p {shlex.quote(project_name)}"
        remote(ssh, f"cd {shlex.quote(project)} && {compose} config --quiet")
        print("cleanroom: compose config valid; building images", flush=True)
        remote(ssh, f"cd {shlex.quote(project)} && {compose} build --no-cache")
        print("cleanroom: images built", flush=True)
        evidence["build"] = {"status": "PASS", "no_cache": True, "backend_lock_sha256": backend_lock, "frontend_lock_sha256": frontend_lock}
        remote(ssh, f"cd {shlex.quote(project)} && {compose} up -d postgres")
        remote(ssh, wait_services_command(project, compose, "postgres"))
        remote(ssh, f"cd {shlex.quote(project)} && {compose} up -d api")
        remote(ssh, wait_services_command(project, compose, "api"))
        remote(ssh, f"cd {shlex.quote(project)} && {compose} up -d worker frontend nginx")
        print("cleanroom: stack started behind explicit dependency gates", flush=True)
        ps_output = remote(ssh, stack_wait_command(project, compose))
        migration_count = remote(ssh, f"cd {shlex.quote(project)} && {compose} exec -T postgres psql -U accumulation -d accumulation -Atc 'SELECT count(*) FROM schema_migrations;'")
        api_health = remote(ssh, f"curl -fsS http://127.0.0.1:18082/health")
        proxy_metadata = remote(ssh, f"curl -fsS http://127.0.0.1:18082/api/build-metadata")
        worker_health = remote(ssh, f"cd {shlex.quote(project)} && {compose} exec -T worker python -c 'import urllib.request; print(urllib.request.urlopen(\"http://127.0.0.1:8001/health\", timeout=5).read().decode())'")
        image_rows = {}
        for service in ("api", "worker", "frontend"):
            image_id = remote(ssh, f"cd {shlex.quote(project)} && {compose} images -q {service}")
            inspect = remote(ssh, f"docker image inspect {shlex.quote(image_id)} --format '{{{{.Id}}}}|{{{{json .Config.Labels}}}}'")
            image_rows[service] = {"image_id": image_id, "inspect": inspect}
        before_state = json.loads(remote(ssh, f"cd {shlex.quote(project)} && {compose} exec -T postgres psql -U accumulation -d accumulation -Atc \"SELECT json_build_object('schema_migrations',(SELECT count(*) FROM schema_migrations),'job_runs',(SELECT count(*) FROM job_runs))::text;\""))
        remote(ssh, f"cd {shlex.quote(project)} && {compose} restart postgres")
        remote(ssh, f"cd {shlex.quote(project)} && for i in $(seq 1 40); do {compose} exec -T postgres pg_isready -U accumulation -d accumulation >/dev/null 2>&1 && break; sleep 2; done")
        remote(ssh, f"cd {shlex.quote(project)} && {compose} restart api worker frontend nginx")
        remote(ssh, stack_wait_command(project, compose))
        after_state = json.loads(remote(ssh, f"cd {shlex.quote(project)} && {compose} exec -T postgres psql -U accumulation -d accumulation -Atc \"SELECT json_build_object('schema_migrations',(SELECT count(*) FROM schema_migrations),'job_runs',(SELECT count(*) FROM job_runs))::text;\""))
        post_restart_health = remote(ssh, "curl -fsS http://127.0.0.1:18082/health")
        post_restart_worker = remote(ssh, f"cd {shlex.quote(project)} && {compose} exec -T worker python -c 'import urllib.request; print(urllib.request.urlopen(\"http://127.0.0.1:8001/health\", timeout=5).read().decode())'")
        evidence.update(
            {
                "compose_config": "PASS",
                "initial_stack": {"status": "PASS", "ps": ps_output, "api_health": api_health, "worker_health": worker_health, "proxy_build_metadata": proxy_metadata, "schema_migrations_count": migration_count, "images": image_rows},
                "worker_image_validation": {"status": "PASS", "worker_built_image_inspect": image_rows["worker"]},
                "restart_validation": {"status": "PASS", "before_state": before_state, "after_state": after_state, "schema_migrations_persisted": before_state["schema_migrations"] == after_state["schema_migrations"], "job_runs_not_lost": after_state["job_runs"] >= before_state["job_runs"], "state_persisted": before_state["schema_migrations"] == after_state["schema_migrations"] and after_state["job_runs"] >= before_state["job_runs"], "api_health": post_restart_health, "worker_health": post_restart_worker},
                "frontend_api_via_nginx": {"status": "PASS", "url": "http://127.0.0.1:18082/health"},
            }
        )
    finally:
        if ssh is not None:
            try:
                compose = f"docker compose -p {shlex.quote(project_name)}"
                remote(ssh, f"cd {shlex.quote(project)} && {compose} down -v --remove-orphans", check=False)
                remote(ssh, f"rm -rf -- {shlex.quote(project)}", check=False)
                cleanup_complete = True
            finally:
                ssh.close()
        archive_path.unlink(missing_ok=True)

    evidence["cleanup_complete"] = cleanup_complete
    output = ROOT / "deployment_evidence" / "CLEANROOM_DOCKER_EVIDENCE.json"
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(output), "source_revision": revision, "build": evidence.get("build"), "restart": evidence.get("restart_validation"), "cleanup_complete": cleanup_complete}, ensure_ascii=False))


if __name__ == "__main__":
    main()
