"""Scan running image filesystems, configs, history, and saved layers for secrets."""
from __future__ import annotations

import json
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path

import paramiko

from scripts.deploy_nas import remote
from scripts.secret_scan import scan_text


PROJECT = "/volume1/docker/tw-accumulation-evidence"
SHELL_SIGNATURES = "eyJ0eXAiOiJKV1Qi|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|" + "FIN" + "MIND_API_TOKEN=[^$]|" + "NAS" + "_PASSWORD=[^$]|postgresql://[^ ]+:[^ ]+@"


def sanitized_findings(output: str, surface: str) -> list[dict[str, str]]:
    findings = scan_text(output, surface)
    if "FINDING" in output:
        findings.extend({"rule": "image_shell_signature", "surface": surface} for _ in range(1))
    return [{"rule": item["rule"], "surface": item["surface"]} for item in findings]


def main() -> None:
    host = os.getenv("NAS_HOST")
    user = os.getenv("NAS_USER")
    password = os.getenv("NAS_PASSWORD")
    if not host or not user or not password:
        raise SystemExit("NAS_HOST, NAS_USER, and NAS_PASSWORD are required through the environment")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=user, password=password, look_for_keys=False, allow_agent=False, timeout=15)
    surfaces: list[dict[str, object]] = []
    try:
        for service, roots in (("api", "/app"), ("worker", "/app"), ("frontend", "/usr/share/nginx/html")):
            image = remote(ssh, f"cd {PROJECT} && docker compose images -q {service} | head -n 1", sudo=True, check=False).strip()
            if not image:
                surfaces.append({"surface": f"image:{service}", "status": "FAIL", "findings": [{"rule": "image_missing", "surface": service}]})
                continue
            filesystem = remote(ssh, f"docker run --rm --entrypoint /bin/sh {shlex.quote(image)} -c {shlex.quote(f'if find {roots} -type f -size -20M -print0 2>/dev/null | xargs -0 -r grep -I -l -E {shlex.quote(SHELL_SIGNATURES)} 2>/dev/null | grep -q .; then echo FINDING; else echo CLEAN; fi')}", sudo=True, check=False)
            history = remote(ssh, f"docker history --no-trunc --format '{{{{.CreatedBy}}}}' {shlex.quote(image)}", sudo=True, check=False)
            config = remote(ssh, f"docker image inspect {shlex.quote(image)}", sudo=True, check=False)
            layer_command = f"tmp=$(mktemp -d); docker save {shlex.quote(image)} -o $tmp/image.tar >/dev/null 2>&1; tar -xf $tmp/image.tar -C $tmp >/dev/null 2>&1; if find $tmp -name layer.tar -print0 | xargs -0 -r -n1 sh -c 'tar -xOf \"$1\" 2>/dev/null' sh | grep -aEq {shlex.quote(SHELL_SIGNATURES)}; then echo FINDING; else echo CLEAN; fi; rm -rf $tmp"
            layers = remote(ssh, layer_command, sudo=True, check=False)
            for suffix, output in (("filesystem", filesystem), ("history", history), ("config", config), ("layers", layers)):
                surface = f"image:{service}:{suffix}"
                findings = sanitized_findings(output, surface)
                surfaces.append({"surface": surface, "status": "FAIL" if findings else "PASS", "findings": findings})
    finally:
        ssh.close()
    result = {"generated_at": datetime.now(timezone.utc).isoformat(), "status": "PASS" if all(item["status"] == "PASS" for item in surfaces) else "FAIL", "surfaces": surfaces, "secret_values_redacted": True, "secrets_included": False}
    output = Path("deployment_evidence/IMAGE_SECRET_EVIDENCE.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(output), "status": result["status"], "secrets_included": False}, ensure_ascii=False))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
