from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "finmind_token_value": re.compile(r"(?i)(?:FINMIND_API_TOKEN|FINMIND_TOKEN)[ \t]*[:=][ \t]*(?!(?:os\.getenv|None|REMOVED|REDACTED)\b)[^\s<>{}\"']{12,}"),
    "nas_password_value": re.compile(r"(?i)NAS_PASSWORD[ \t]*[:=][ \t]*(?!(?:os\.getenv|None|REMOVED|REDACTED)\b)[^\s<>{}\"']{8,}"),
    "bearer_value": re.compile(r"(?i)authorization\s*[:=]\s*bearer\s+[A-Za-z0-9._-]{12,}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "database_url_credential": re.compile(r"(?i)(?:postgres(?:ql)?|mysql)://[^\s:@]+:[^\s@]+@"),
}
EXCLUDE_PARTS = {".git", ".venv", "node_modules", "data", "postgres"}


def tracked_files() -> list[Path]:
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8")
    return [ROOT / value for value in output.split("\0") if value]


def _excluded(path: Path) -> bool:
    return any(part == ".env" or part == "secrets" or part.startswith("review_bundle_") and path.suffix not in {".zip"} or part in EXCLUDE_PARTS for part in path.parts)


def _scan_file(path: Path, display: str, findings: list[dict[str, str]]) -> None:
    try:
        content = path.read_bytes()
    except OSError:
        return
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        # Binary artefacts are still checked for credential signatures.
        text = content.decode("latin-1")
    for name, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            findings.append({"rule": name, "path": display, "line": str(text[:match.start()].count("\n") + 1)})


def scan(extra_paths: list[Path] | None = None) -> dict[str, object]:
    findings: list[dict[str, str]] = []
    paths = tracked_files()
    for root in extra_paths or []:
        if root.exists():
            paths.extend([root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()])
    seen: set[Path] = set()
    for path in paths:
        if path in seen or _excluded(path):
            continue
        seen.add(path)
        _scan_file(path, str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path), findings)
        if path.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(path) as archive:
                    for member in archive.infolist():
                        if member.is_dir():
                            continue
                        payload = archive.read(member)
                        temporary = ROOT / ".secret_scan_tmp"
                        temporary.write_bytes(payload)
                        _scan_file(temporary, f"{path.name}!{member.filename}", findings)
                        temporary.unlink(missing_ok=True)
            except (OSError, zipfile.BadZipFile):
                pass
    return {"status": "PASS" if not findings else "FAIL", "scanned_files": len(seen), "rules": sorted(PATTERNS), "findings": findings, "secret_values_redacted": True}


if __name__ == "__main__":
    result = scan([Path(value) for value in sys.argv[1:] if not value.startswith("-")])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
