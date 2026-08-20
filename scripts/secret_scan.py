from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "finmind_token_value": re.compile(r"(?i)(?:FINMIND_API_TOKEN|FINMIND_TOKEN)[ \t]*[:=][ \t]*(?!(?:os\.getenv|None|REMOVED|REDACTED)\b)[^\s<>{}\"']{12,}"),
    "nas_password_value": re.compile(r"(?i)NAS_PASSWORD[ \t]*[:=][ \t]*(?!(?:os\.getenv|None|REMOVED|REDACTED)\b)[^\s<>{}\"']{8,}"),
    "bearer_value": re.compile(r"(?i)authorization\s*[:=]\s*bearer\s+[A-Za-z0-9._-]{12,}"),
}
EXCLUDE_PARTS = {".git", ".venv", "node_modules", "dist", "data", "review_bundle_"}


def tracked_files() -> list[Path]:
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8")
    return [ROOT / value for value in output.split("\0") if value]


def scan() -> dict[str, object]:
    findings: list[dict[str, str]] = []
    for path in tracked_files():
        if any(part.startswith("review_bundle_") or part in EXCLUDE_PARTS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                findings.append({"rule": name, "path": str(path.relative_to(ROOT)), "line": str(text[:match.start()].count("\n") + 1)})
    return {"status": "PASS" if not findings else "FAIL", "scanned_tracked_files": len(tracked_files()), "findings": findings}


if __name__ == "__main__":
    result = scan()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
