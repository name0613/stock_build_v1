"""Stage/roll out the reviewed refresh policy fix without rotating credentials.

Requires NAS_HOST/NAS_USER/NAS_PASSWORD in the environment. Builds from committed
Git sources and the verified running dependency layer; never uploads secrets.
Run `stage` after frontend production build, then `rollout`. Refresh counters
are preserved unless both phases explicitly use --reset-exclusions.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile

import paramiko

from deploy_nas import ROOT, remote, detect_sftp_chroot, to_sftp_path
from backend.app.calendar import CALENDAR_HASH
from backend.app.scoring import FORMULA_HASH

PROJECT = "/volume1/docker/tw-accumulation-evidence"
EVIDENCE = ROOT / "deployment_evidence/REFRESH_POLICY_FIX_20261003.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["stage", "rollout"])
    parser.add_argument("--reset-exclusions", action="store_true")
    parser.add_argument("--evidence", type=Path, default=EVIDENCE)
    args = parser.parse_args()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    release = f"/volume1/docker/tw-refresh-policy-release-{revision[:12]}"
    backup = f"/volume1/docker/tw-refresh-policy-backup-{revision[:12]}"
    evidence_path = args.evidence
    evidence = json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path.exists() else {}
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(os.environ.get("NAS_HOST", "192.168.31.138"), username=os.environ["NAS_USER"], password=os.environ["NAS_PASSWORD"], look_for_keys=False, allow_agent=False, timeout=15)
    try:
        def run(command, sudo=True):
            return remote(ssh, command, sudo=sudo)
        def save():
            evidence_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if args.phase == "stage":
            paths = ["backend", "fixtures", "migrations", "scripts", "ARCHITECTURE.md", "SCORING.md", "docker-compose.yml", "nginx", "frontend/src", "frontend/package.json", "frontend/package-lock.json", "frontend/tsconfig.json", "frontend/vite.config.ts", "frontend/index.html", "frontend/Dockerfile", "docs", "README.md", "OPERATIONS.md"]
            if subprocess.check_output(["git", "status", "--porcelain", "--", *paths], cwd=ROOT, text=True).strip():
                raise RuntimeError("commit reviewed application changes before staging")
            run(f"test -d {PROJECT} && mkdir -p {release}", sudo=False)
            api = run(f"cd {PROJECT} && docker compose ps -a -q api")
            base_image = run(f"docker inspect --format '{{{{.Image}}}}' {api}")
            deployed = json.loads(run(f"docker run --rm --entrypoint cat {base_image} /app/build-metadata.json"))
            backend_lock = hashlib.sha256((ROOT / "backend/requirements.lock").read_bytes()).hexdigest()
            if deployed["backend_lock_sha256"] != backend_lock:
                raise RuntimeError("running dependencies differ; full dependency build required")
            dependency_tag = f"tw-refresh-policy-dependencies:{base_image.split(':')[-1][:16]}"
            run(f"docker tag {base_image} {dependency_tag}")
            stamp = datetime.now(timezone.utc).isoformat()
            metadata = {"source_revision": revision, "backend_lock_sha256": backend_lock, "score_spec_hash": FORMULA_HASH, "calendar_hash": CALENDAR_HASH, "build_timestamp": stamp}
            front_metadata = {"source_revision": revision, "frontend_lock_sha256": hashlib.sha256((ROOT / "frontend/package-lock.json").read_bytes()).hexdigest(), "build_timestamp": stamp}
            with tempfile.TemporaryDirectory(prefix="refresh-policy-release-") as tmp:
                tmp = Path(tmp)
                subprocess.run(["git", "archive", "--format=tar", "--output", str(tmp / "source.tar"), revision, *paths], cwd=ROOT, check=True)
                with tarfile.open(tmp / "frontend-dist.tar", "w") as archive:
                    for path in (ROOT / "frontend/dist").rglob("*"):
                        if path.is_file():
                            archive.add(path, arcname="frontend-dist/" + path.relative_to(ROOT / "frontend/dist").as_posix())
                (tmp / "build-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
                (tmp / "frontend-metadata.json").write_text(json.dumps(front_metadata), encoding="utf-8")
                labels = f'LABEL org.opencontainers.image.revision="{revision}" org.opencontainers.image.created="{stamp}" org.openai.calendar-sha256="{CALENDAR_HASH}"\n'
                (tmp / "Dockerfile.backend").write_text(f"FROM {dependency_tag}\nUSER root\n" + labels + "COPY --chown=app:app backend/app /app/app\nCOPY --chown=app:app backend/tests /app/tests\nCOPY --chown=app:app scripts /app/scripts\nCOPY --chown=app:app migrations /app/migrations\nCOPY --chown=app:app fixtures /app/fixtures\nCOPY --chown=app:app ARCHITECTURE.md SCORING.md build-metadata.json /app/\nUSER app\n", encoding="utf-8")
                (tmp / "Dockerfile.frontend").write_text("FROM nginx:1.27-alpine\n" + labels + "COPY frontend-dist /usr/share/nginx/html\nCOPY frontend-metadata.json /usr/share/nginx/html/build-metadata.json\nRUN chmod -R a+rX /usr/share/nginx/html\n", encoding="utf-8")
                sftp = ssh.open_sftp()
                prefix = detect_sftp_chroot(sftp, PROJECT)
                for path in tmp.iterdir():
                    sftp.put(str(path), to_sftp_path(f"{release}/{path.name}", prefix))
                sftp.close()
            run(f"tar -xf {release}/source.tar -C {release} && tar -xf {release}/frontend-dist.tar -C {release}", sudo=False)
            print("Building backend from committed source and verified dependencies", flush=True)
            run(f"docker build -f {release}/Dockerfile.backend -t tw-refresh-policy-backend:{revision[:12]} {release}")
            print("Building frontend from validated production assets", flush=True)
            run(f"docker build -f {release}/Dockerfile.frontend -t tw-refresh-policy-frontend:{revision[:12]} {release}")
            probe = run(f"docker run --rm --entrypoint python tw-refresh-policy-backend:{revision[:12]} -c " + shlex.quote("from app.calendar import *; assert not is_trading_session(date(2026,9,28)); print(CALENDAR_HASH)"))
            assert probe == CALENDAR_HASH
            prior_attempt = evidence if evidence.get("reset_receipt") else evidence.get("prior_attempt")
            evidence = {"source_revision": revision, "release_directory": release, "rollback_directory": backup, "build_metadata": metadata, "frontend_metadata": front_metadata, "verified_dependency_image": base_image, "stage_completed_at": datetime.now(timezone.utc).isoformat(), "secrets_included": False}
            evidence["reset_exclusions"] = args.reset_exclusions
            if prior_attempt:
                evidence["prior_attempt"] = prior_attempt
            save()
            print("Stage complete", flush=True)
            return
        if evidence.get("source_revision") != revision or not evidence.get("stage_completed_at"):
            raise RuntimeError("stage this exact revision first")
        if evidence.get("reset_exclusions", False) != args.reset_exclusions:
            raise RuntimeError("reset policy must match the staged release")
        if evidence.get("rollout_completed_at"):
            print("This rollout is already complete; use verification without resetting again")
            return
        run(f"mkdir -p {backup} && chmod 700 {backup}")
        old_images = {}
        for service in ("api", "worker", "frontend"):
            cid = run(f"cd {PROJECT} && docker compose ps -a -q {service}")
            old_images[service] = run(f"docker inspect --format '{{{{.Image}}}}' {cid}")
            run(f"docker tag {old_images[service]} tw-refresh-policy-rollback-{service}:{revision[:12]}")
        run(f"cd {PROJECT} && tar -czf {backup}/application.tar.gz backend scripts migrations fixtures frontend/src docker-compose.yml nginx .env DEPLOYED_SOURCE_REVISION && chmod 600 {backup}/application.tar.gz")
        evidence["rollback_images"] = old_images
        save()
        print("Stopping old API and worker before release activation", flush=True)
        run(f"cd {PROJECT} && docker compose stop -t 30 worker api")
        run(f"tar -xf {release}/source.tar -C {PROJECT}")
        sftp = ssh.open_sftp()
        prefix = detect_sftp_chroot(sftp, PROJECT)
        env_path = to_sftp_path(f"{PROJECT}/.env", prefix)
        with sftp.file(env_path, "r") as handle:
            env_lines = handle.read().decode("utf-8").splitlines()
        values = {"SOURCE_REVISION": revision, "BACKEND_LOCK_SHA256": evidence["build_metadata"]["backend_lock_sha256"], "FRONTEND_LOCK_SHA256": evidence["frontend_metadata"]["frontend_lock_sha256"], "SCORE_SPEC_HASH": FORMULA_HASH, "CALENDAR_HASH": CALENDAR_HASH, "BUILD_TIMESTAMP": evidence["build_metadata"]["build_timestamp"]}
        kept = [line for line in env_lines if line.split("=", 1)[0] not in values]
        with sftp.file(env_path, "w") as handle:
            handle.write("\n".join(kept + [f"{key}={value}" for key, value in values.items()]) + "\n")
        sftp.chmod(env_path, 0o600)
        with sftp.file(to_sftp_path(f"{PROJECT}/DEPLOYED_SOURCE_REVISION", prefix), "w") as handle:
            handle.write(revision + "\n")
        sftp.close()
        for service in ("api", "worker"):
            run(f"docker tag tw-refresh-policy-backend:{revision[:12]} tw-accumulation-evidence-{service}:latest")
        run(f"docker tag tw-refresh-policy-frontend:{revision[:12]} tw-accumulation-evidence-frontend:latest")
        if args.reset_exclusions:
            reset_id = (evidence.get("prior_attempt", {}).get("reset_receipt") or {}).get("reset_id", f"calendar-policy-{revision[:12]}")
            output = run(f"cd {PROJECT} && docker compose run --rm --no-deps api python /app/scripts/reset_refresh_issues.py --reset-id {reset_id}")
            evidence["reset_receipt"] = json.loads(output)
            save()
            print(json.dumps(evidence["reset_receipt"]), flush=True)
        run(f"cd {PROJECT} && docker compose up -d --no-build --force-recreate api worker frontend nginx")
        evidence["rollout_completed_at"] = datetime.now(timezone.utc).isoformat()
        save()
        print("Rollout complete; verify health, source hashes and new refresh progress", flush=True)
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
