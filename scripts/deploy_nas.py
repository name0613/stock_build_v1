"""SSH deployment helper. Password/token are read from environment and never printed or written to source."""
from __future__ import annotations

import os
import posixpath
import secrets
import shlex
import subprocess
import hashlib
from datetime import datetime, timezone
import sys
from pathlib import Path

try:
    import paramiko
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Install paramiko before deployment: python -m pip install paramiko") from exc

ROOT = Path(__file__).resolve().parents[1]
SOURCE_REVISION = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
BACKEND_LOCK_SHA256 = hashlib.sha256((ROOT / "backend/requirements.lock").read_bytes()).hexdigest()
FRONTEND_LOCK_SHA256 = hashlib.sha256((ROOT / "frontend/package-lock.json").read_bytes()).hexdigest()
sys.path.insert(0, str(ROOT))
from backend.app.calendar import CALENDAR_HASH  # noqa: E402
from backend.app.scoring import FORMULA_HASH  # noqa: E402
BUILD_TIMESTAMP = datetime.now(timezone.utc).isoformat()
HOST = os.getenv("NAS_HOST", "192.168.31.138")
USER = os.getenv("NAS_USER")
PASSWORD = os.getenv("NAS_PASSWORD")
FINMIND_TOKEN = os.getenv("FINMIND_API_TOKEN")
REMOTE_CANDIDATES = ["/volume1/docker", "/share/Container", "/share/CACHEDEV1_DATA/Container", "/mnt/user/appdata"]
EXCLUDED = {".git", ".venv", "node_modules", "dist", "data", "secrets", "test-results", "__pycache__", ".pytest_cache", ".ruff_cache", "review_bundle_"}


def remote(
    client: paramiko.SSHClient,
    command: str,
    *,
    check: bool = True,
    sudo: bool = False,
    input_text: str | None = None,
) -> str:
    if sudo:
        if not PASSWORD:
            raise RuntimeError("NAS password is required for sudo-backed Docker commands")
        command = f"sudo -S -p '' sh -c {shlex.quote(command)}"
    stdin, stdout, stderr = client.exec_command(command)
    if sudo:
        stdin.write(PASSWORD + "\n")
    if input_text:
        stdin.write(input_text)
    if sudo or input_text:
        stdin.flush()
        stdin.channel.shutdown_write()
    output = stdout.read().decode("utf-8", "replace")
    error = stderr.read().decode("utf-8", "replace")
    if check and stdout.channel.recv_exit_status() != 0:
        raise RuntimeError(f"remote command failed: {command.split()[0] if command else 'command'}: {error[:300]}")
    return output.strip()


def choose_root(client: paramiko.SSHClient) -> str:
    checks = "; ".join(f"if [ -d {path} ]; then printf '%s\\n' {path}; fi" for path in REMOTE_CANDIDATES)
    candidates = [line for line in remote(client, checks, check=False).splitlines() if line]
    if not candidates:
        raise RuntimeError("No verified NAS container volume was found; deployment stopped without guessing a path")
    return candidates[0]


def iter_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED or part.startswith("review_bundle_") for part in relative.parts):
            continue
        if path.name in {".env", "postgres_password"}:
            continue
        files.append(path)
    return files


def detect_sftp_chroot(sftp: paramiko.SFTPClient, shell_project: str) -> str:
    """Map shell-visible absolute paths to the NAS SFTP chroot namespace."""
    for prefix in ("/volume1", "/share", "/mnt/user"):
        if not shell_project.startswith(prefix + "/"):
            continue
        candidate = shell_project[len(prefix) :]
        try:
            sftp.listdir(candidate)
        except OSError:
            continue
        return prefix
    try:
        sftp.listdir(shell_project)
    except OSError as exc:
        raise RuntimeError("NAS shell path is not addressable through its SFTP namespace") from exc
    return ""


def to_sftp_path(shell_path: str, chroot_prefix: str) -> str:
    return shell_path[len(chroot_prefix) :] if chroot_prefix and shell_path.startswith(chroot_prefix) else shell_path


def sync_database_password(client: paramiko.SSHClient, project: str, database_password: str) -> None:
    command = f"cd {project} && docker compose exec -T postgres psql -U accumulation -d accumulation -v ON_ERROR_STOP=1"
    remote(client, command, sudo=True, input_text=f"ALTER USER accumulation PASSWORD '{database_password}';\n")


def main() -> None:
    if not USER or not PASSWORD:
        raise SystemExit("NAS_USER and NAS_PASSWORD must be provided by the execution environment")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, look_for_keys=False, allow_agent=False, timeout=15)
    try:
        print("NAS preflight:")
        preflight_commands = [
            ("uname -srm", False),
            ("uname -m", False),
            ("docker --version", False),
            ("docker compose version", False),
            ("df -h", False),
            ("(ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null)", False),
            ("docker ps --format '{{.Names}} {{.Status}}'", True),
            ("docker volume ls", True),
            ("docker network ls", True),
        ]
        for command, needs_sudo in preflight_commands:
            print(remote(ssh, command, check=False, sudo=needs_sudo))
        base = choose_root(ssh)
        project = posixpath.join(base, "tw-accumulation-evidence")
        remote(ssh, f"mkdir -p {project}/secrets {project}/data/raw")
        sftp = ssh.open_sftp()
        sftp_prefix = detect_sftp_chroot(sftp, project)
        for file in iter_files():
            relative = file.relative_to(ROOT).as_posix()
            target = posixpath.join(project, relative)
            parent = posixpath.dirname(target)
            remote(ssh, f"mkdir -p {parent}")
            sftp.put(str(file), to_sftp_path(target, sftp_prefix))
        env_path = to_sftp_path(posixpath.join(project, ".env"), sftp_prefix)
        token_path = to_sftp_path(posixpath.join(project, "secrets/finmind_api_token"), sftp_prefix)
        revision_path = to_sftp_path(posixpath.join(project, "DEPLOYED_SOURCE_REVISION"), sftp_prefix)
        revision_file = sftp.file(revision_path, "w")
        revision_file.write(SOURCE_REVISION + "\n")
        revision_file.close()
        sftp.chmod(revision_path, 0o600)
        if FINMIND_TOKEN:
            token_file = sftp.file(token_path, "w")
            token_file.write(FINMIND_TOKEN + "\n")
            token_file.close()
            sftp.chmod(token_path, 0o600)
            env_file = sftp.file(env_path, "w")
            env_file.write("# Production credentials are mounted as Compose secrets.\n")
            env_file.write(f"SOURCE_REVISION={SOURCE_REVISION}\nBACKEND_LOCK_SHA256={BACKEND_LOCK_SHA256}\nFRONTEND_LOCK_SHA256={FRONTEND_LOCK_SHA256}\nSCORE_SPEC_HASH={FORMULA_HASH}\nCALENDAR_HASH={CALENDAR_HASH}\nBUILD_TIMESTAMP={BUILD_TIMESTAMP}\n")
            env_file.close()
            sftp.chmod(env_path, 0o600)
        else:
            try:
                sftp.stat(token_path)
            except OSError as exc:
                raise RuntimeError("FINMIND_API_TOKEN is required for the first NAS deployment") from exc
            env_file = sftp.file(env_path, "w")
            env_file.write("# Production credentials are mounted as Compose secrets.\n")
            env_file.write(f"SOURCE_REVISION={SOURCE_REVISION}\nBACKEND_LOCK_SHA256={BACKEND_LOCK_SHA256}\nFRONTEND_LOCK_SHA256={FRONTEND_LOCK_SHA256}\nSCORE_SPEC_HASH={FORMULA_HASH}\nCALENDAR_HASH={CALENDAR_HASH}\nBUILD_TIMESTAMP={BUILD_TIMESTAMP}\n")
            env_file.close()
            sftp.chmod(env_path, 0o600)
        secret_path = to_sftp_path(posixpath.join(project, "secrets/postgres_password"), sftp_prefix)
        try:
            sftp.stat(secret_path)
        except OSError:
            secret_file = sftp.file(secret_path, "w")
            secret_file.write(secrets.token_urlsafe(24) + "\n")
            secret_file.close()
        sftp.chmod(secret_path, 0o600)
        secret_file = sftp.file(secret_path, "r")
        database_password = secret_file.read().decode("utf-8").strip()
        secret_file.close()
        sftp.close()
        remote(ssh, f"cd {project} && docker compose build", sudo=True)
        remote(
            ssh,
            f"cd {project} && docker compose up -d postgres && "
            "for i in $(seq 1 30); do "
            "docker compose exec -T postgres pg_isready -U accumulation -d accumulation >/dev/null 2>&1 && break; "
            "sleep 2; done",
            sudo=True,
        )
        sync_database_password(ssh, project, database_password)
        remote(ssh, f"cd {project} && docker compose up -d", sudo=True)
        remote(ssh, f"cd {project} && docker compose up -d --force-recreate nginx", sudo=True)
        print(remote(ssh, f"cd {project} && docker compose ps", sudo=True))
        print(remote(ssh, f"cd {project} && docker compose ps --format json", sudo=True))
        print(f"NAS deployment completed at {project}; credentials were not printed")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
